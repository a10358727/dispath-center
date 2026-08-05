# Implementation Progress

> Current adoption gate after `execution_plan_materialized`: `70` catalog entries; the
> historical entries below retain their original counts where applicable.

## 2026-08-06 — WP-2D report binds attempts to the candidate approval

- `scripts/canary_report.py` now joins every scoped SSH attempt to its
  immutable approval and requires the `enqueue-execution-v1` /
  `wp2d-ssh-canary-v2` contract to carry one exact candidate commit matching
  the evidence manifest. Missing approvals, malformed payloads and mixed
  candidate revisions fail closed; payload contents are not emitted in the
  report.
- The historical d73a38e canary still passes this stronger check (20/20
  attempts bind the same candidate). The focused evaluator suite is now
  `12 passed`; this remains historical evidence and does not promote the
  current branch.

## 2026-08-06 — Historical WP-2D v2 canary pass recorded

- A stable SQLite online-backup copy was evaluated with
  `scripts/canary_report.py` for candidate
  `d73a38e33b328ce12cae0921c7f5a63242322402` on the designated non-production
  `worker_5090_117`. The exact `ssh-canary-evidence-v2` window was eight hours
  and contained 20 terminal attempts, 20 delivered collection operations, zero
  duplicate launches, zero false failures, zero lost terminals, zero
  unresolved unknowns and zero uncertain operations.
- Forced response loss, control-plane restart and rollback-to-legacy-SSH drill
  manifests all passed. The committed summary is
  `docs/evidence/WP2D_V2_20260802_D73A38E.md`; raw runtime evidence remains
  outside Git and the evaluator returned exit code `0`.
- This resolves `RB-LAUNCH-001` only for that exact historical candidate. The
  current branch contains later execution-path changes, so the current
  candidate remains unproven and must run a fresh eight-hour window before any
  rollout or capability status is promoted.

## 2026-08-06 — closed dynamic audit-writer inventory

- The adoption CI gate now expands the closed dynamic action families used by
  `approval.kind`, server removal `action_name`, and authorization-shadow
  `item.audit_action`. Every value in those contracts must resolve to the typed
  adoption catalog; an open dynamic expression is still left unclassified rather
  than guessed.
- The prior 69 catalog-entry count and 16 legacy compatibility families remain
  historical evidence for that slice; this slice adds one bounded durable plan
  materialization owner. It does not claim that legacy JSONL summaries or
  external production/canary gates are migrated.

## 2026-08-06 — ExecutionPlan materialization durable UoW

- A run request now commits the immutable `execution_plans` row, its pending
  `plan_run` approval, `approval_created`, and bounded
  `execution_plan_materialized` evidence in one immediate SQLite transaction.
  The event binds the plan/approval and only records the command digest,
  contract/binding booleans, and reproducibility; command text and other
  payload contents are not copied into durable parameters.
- If the plan event append fails, both the plan and approval roll back together.
  The existing `plan_run_request` JSONL line remains a compatibility summary,
  and approve-time revalidation/immutable plan pinning are unchanged.

## 2026-08-06 — Audit export dead-letter alert evidence

- Durable audit export telemetry and the read-only
  `scripts/audit_export_status.py` gate now share one alert projection:
  `status=attention` for any backlog, and `alert.active=true` with
  `severity=critical` and `dead_letter_present` when a row exhausts retries.
  This keeps the signal deterministic and prevents a local threshold from
  being mistaken for an approved production SLO.
- `/operations/metrics` exposes the same `audit.export_outbox.alert` object.
  The signal is read-only and does not replay or clear rows; external
  notification routing, alert ownership, and the production dead-letter gate
  remain explicitly open.

## 2026-08-06 — Node enrollment approval UoW

- Approval-gated `node_enroll` now generates the one-time credential in the
  approval layer, then commits the node row, bounded `node_enrolled` event,
  approval note/status, and `approval_decided` in one SQLite transaction.
  Approval-gated `node_revoke` likewise commits the node security hold,
  affected-attempt liveness changes, `node_revoked`, and `approval_decided` in
  one UoW. The durable envelope never receives raw tokens, secret digests, or
  credential material.
- If either lifecycle append fails, the approval remains pending and the node/
  attempt projection rolls back. Direct `insert_node()`/`enroll_node()` and
  non-approval revoke callers keep their existing primitive behavior; only the
  approval paths use the new atomic boundaries.

## 2026-08-06 — Node retirement and rotation approval UoW

- Approval-gated `node_retire` actions (`start_drain`, `resume_assignment`, and
  `complete_retirement`) now commit the node state transition, bounded lifecycle
  event, and `approval_decided` in one SQLite transaction. The transaction
  revalidates the immutable node/server/action payload and pending status.
- Legacy and staged `node_rotate` approvals now use the same boundary for the
  credential projection, `node_rotated`, and `approval_decided`. Staged
  activation still keeps the primary credential unchanged until the separate
  token/nonce activation exchange; explicit `replace_pending` remains the
  response-loss recovery path.
- Injected failures for the lifecycle event or approval decision leave the
  approval pending and restore the prior Node credential/drain/retirement
  projection. Direct non-approval DB and registry callers retain their existing
  optional-approval behavior.

## 2026-08-06 — Legacy scheduler dispatch state UoW

- The retained SSH scheduler fallback now records `queued→running` claims as
  `execution_job_dispatched` and pre-effect dispatch exceptions that return a
  Job to `queued` as `execution_job_dispatch_requeued`, inside the existing
  `update_job()` immediate transaction. Local sync dispatch uses the same
  bounded events with a distinct backend label; pre-dispatch disk rejection
  uses the existing terminal event contract.
- The historical `dispatch`/`dispatch_failed` JSONL lines remain compatibility
  summaries, while durable parameters contain only Job/server/backend and a
  bounded reason code—never commands, paths, exception text, or remote output.
  Audit append failure now rolls the corresponding scheduler state transition
  back before any remote dispatch can begin; the SSH fallback and its existing
  response-loss behavior are otherwise unchanged.

## 2026-08-06 — Scheduler stalled-state durable UoW

- A `stalled_suspect` transition now commits the bounded
  `execution_job_stall_state_recorded` event with the Job/server identity,
  previous/current status, transition reason, and boolean state in the same
  immediate Job transaction. Repeated observations with no transition stay
  quiet; the existing `stall_suspect` JSONL line remains a compatibility
  notification summary.
- Durable append failure rolls back the log-size/flag projection before the
  notification callback or compatibility summary runs. Clearing after log
  growth emits a separate idempotent `cleared` result; Job execution status is
  never changed by stall detection.

## 2026-08-06 — Unbound coding-run result UoW

- The retained unbound coding-task result collector now commits the
  `coding_runs` projection and a bounded `coding_run_result_recorded` durable
  event in the same immediate transaction. The event carries only Job/run
  identity, terminal status, result-commit presence, approval correlation, and
  the system actor; result content, paths, commands, and error text remain
  outside durable parameters.
- A durable append failure rolls the CodingRun result projection back, so a
  result cannot appear canonical without its ledger evidence. The historical
  `coding_finished` JSONL line remains a compatibility summary for the legacy
  wrapper; bound Engineering Task results retain their existing
  `engineering_task_result_recorded` event.

## 2026-08-05 — coding-run cleanup intent/outcome boundary

- The compensating cleanup endpoint now writes a path-free
  `engineering_task_cleanup_intent` before deleting the Runner task
  directory. The intent binds the coding run, approval, runner, prune choice,
  and a contract digest without copying workspace/source paths into durable
  parameters.
- Successful remote cleanup commits the local `worktree_path` projection and
  `engineering_task_cleanup_outcome(applied)` in one SQLite UoW. Response loss
  or a durable-finalization failure records `unknown`, leaves the local
  projection intact, and returns a manual-recovery response; a retry performs
  no SSH call and never replays the deletion.
- The historical `coding_cleanup` JSONL line remains a compatibility summary;
  durable events carry actor attribution when the request is authenticated.

## 2026-08-05 — server attempt filesystem preflight durable observation

- The exact active approved SSH revision's filesystem evidence and bounded
  `server_attempt_backend_preflight_recorded` event now commit in one SQLite
  UoW. A durable append failure rolls the revision evidence back, so an
  eligible/ineligible/unknown observation cannot exist without ledger evidence.
- The durable envelope contains only server/revision identity, preflight
  status, classifier reason, filesystem type, and contract version. Remote
  stdout, key paths, and exception text remain excluded; the historical
  `server_attempt_backend_preflight` JSONL line remains an explicit
  compatibility summary.
- Request actor attribution is passed into the durable event, while direct
  setup callers retain the existing optional-actor compatibility behavior.

## 2026-08-05 — `server_bootstrap` approval intent/outcome boundary

- Approval-gated bootstrap now claims a payload/script-digest-bound
  `server_bootstrap_intent` before uploading or executing the fixed script.
  The intent stores only target identity and digests; key material, capability
  output, and report contents remain outside durable parameters.
- The bootstrap report row, bounded `server_bootstrap_outcome`, and
  `approval_decided` now commit in one SQLite UoW. If durable append fails,
  the report and approval decision roll back while the approval remains
  pending with an `unknown` outcome.
- SSH/write response loss records `unknown` and leaves the approval pending;
  a retry refuses to re-upload or re-execute the script and requires explicit
  operator recovery. The historical full report JSONL summary remains a
  compatibility projection.

## 2026-08-05 — `hub_sync` remote intent/outcome boundary

- Direct `hub_sync` now records a correlation-scoped
  `project_hub_sync_intent` before worker bundle creation, rsync, or local hub
  fetch. The operation id is explicit for retries; the durable envelope binds
  only project/server/instance identity and a payload digest, never paths,
  bundle bytes, or command output.
- Successful sync commits the immutable `ProjectVersion` (when a full HEAD is
  available) and `project_hub_sync_outcome` in one SQLite UoW. Known command
  failures record a bounded `failed` outcome; transport/response loss records
  `unknown` without pretending the remote state is absent.
- Repeating an operation id is idempotent: the same intent can converge from
  `unknown` to `applied`, and an existing `(project, git_commit)` version is
  reused. The historical `hub_sync` JSONL line remains a compatibility summary.

## 2026-08-05 — `apply_patch` remote intent/outcome boundary

- Approval-gated `apply_patch` now records a payload-digest-bound
  `project_apply_patch_intent` immediately before the first project branch
  mutation. The pinned instance/branch identity prevents a retry from
  rewriting the diff or replaying checkout/apply/commit after response loss.
- Applied outcomes commit the bounded
  `project_apply_patch_outcome` and `approval_decided` in one SQLite UoW;
  durable append failure leaves the approval pending and records only an
  `unknown` outcome. The existing full-diff JSONL summary remains a
  compatibility projection, while durable parameters exclude diff content and
  remote output.
- SSH response loss leaves the approval pending. A later approval attempt
  reads only `refs/heads/{new_branch}` and can finalize the same intent without
  replaying any write command; missing/conflicting evidence stays unknown.

## 2026-08-05 — `git_init` remote intent/outcome boundary

- `git_init` now records an immutable, payload-digest-bound
  `project_git_init_intent` before the first mutating SSH command. A pending
  approval is claimed once, so a retry cannot blindly replay `git init` after
  a response-loss window.
- Successful initialization and size-guard compensation commit the instance
  git projection, bounded `project_git_init_outcome`, and `approval_decided`
  in one SQLite UoW. Durable append failure rolls the local projection back
  while the approval remains pending.
- SSH exceptions are represented as an `unknown` outcome and leave the
  approval pending; a later read-only HEAD/branch reconcile can finalize the
  same intent. Existing SSH commands and the `git_init` JSONL compatibility
  summary remain unchanged, and no gitignore/path/remote output enters the
  durable envelope.

## 2026-08-05 — `project_deploy` remote intent/outcome boundary

- Project deploy now claims a payload-digest-bound
  `project_deploy_intent` before local bundle creation, rsync, or target clone;
  retries cannot silently repeat a partially observed deploy.
- Applied deploys commit the new `project_instance`, canonical
  `ProjectVersion` (when a HEAD is observed), `project_deploy_outcome`, and
  `approval_decided` in one UoW. Known command failures retain the existing
  rejected/cleanup guidance while recording bounded durable outcome evidence.
- SSH response loss leaves the approval pending with an `unknown` outcome;
  the next approval attempt uses read-only target HEAD/branch evidence to
  finalize without replaying bundle/clone commands. Destination paths and raw
  bundle/command output stay out of durable parameters.

## 2026-08-05 — Canonical ProjectVersion durable lifecycle

- Creating a new immutable `(project, git_commit)` ProjectVersion now uses an
  immediate transaction and emits a bounded `project_version_created` event;
  the event carries only project/commit/ref identity and source-instance
  presence, never version metadata content.
- Repeated hub-sync/deploy observations of an existing commit remain
  idempotent and do not rewrite the original ref/source/metadata or append a
  second lifecycle event. Project-deploy-created versions carry the approval
  correlation; direct hub-sync versions retain request actor attribution when
  available.
- Audit append failure rolls the version row back, while remote hub/deploy
  behavior and the existing compatibility JSONL summaries remain unchanged.

## 2026-08-05 — Inventory scan and nested-candidate batch UoW

- Approved `inventory_scan` now keeps the remote read-only scan outside the
  database transaction, then persists every candidate upsert, bounded
  candidate lifecycle event, and `approval_decided` in one `BEGIN IMMEDIATE`
  unit of work. An audit append failure no longer leaves a partially imported
  candidate set; the approval remains pending and the scan can be retried.
- Approved `ignore_nested_candidates` now rechecks each candidate under one
  transaction, preserves the existing pending-state competition skips, and
  commits all `project_candidate_status_changed` events with the approval
  decision. The `candidates_ignore_nested` JSONL line remains a compatibility
  summary and is not authoritative.
- Successful, unchanged-rescan, skip-race, and audit-failure rollback tests
  cover both batch boundaries; no remote paths or command output enter durable
  parameters.

## 2026-08-05 — Project candidate/import durable boundary

- Candidate inventory upsert and status transitions now use an immediate
  transaction with bounded `project_candidate_created`,
  `project_candidate_updated`, and `project_candidate_status_changed` events.
  Repeated scans with unchanged metadata and repeated status writes remain
  quiet; paths, README excerpts, embedded-data paths, command guesses, and
  remote content are not copied into durable parameters.
- Project-instance create/upsert now emits bounded
  `project_instance_created`/`project_instance_updated` events only when
  material metadata changes. The stable row identity and existing
  `project_id` repair behavior remain unchanged.
- Approved `import_project` and `ignore_project_candidate` decisions now use
  one `BEGIN IMMEDIATE` UoW for project/instance/candidate mutations,
  `approval_decided`, and their durable lifecycle events. Audit append failure
  rolls every mutation back while leaving the approval pending; the existing
  JSONL summaries remain compatibility evidence.

## 2026-08-05 — Project instance reconcile durable boundary

- Read-only project-instance observations now commit state/git snapshot
  changes with a bounded `project_instance_reconciled` durable event in the
  same `BEGIN IMMEDIATE` transaction. The event carries only project/server
  identity, from/to state, and whether the instance was observed; paths,
  command output, and remote errors remain excluded.
- Repeated observations with no state or snapshot change do not create an
  unbounded event stream, while `last_seen` still advances for real sightings.
  Unknown/offline and missing semantics remain unchanged, and the existing
  `instance_reconcile` JSONL summary remains compatibility evidence.
- State transition correlation and audit-failure rollback tests cover both
  successful reconciliation and no-partial-row behavior.

## 2026-08-05 — Dataset cache reconcile UoW

- Remote `ls` observations now reconcile one server's dataset cache in a
  single `BEGIN IMMEDIATE` unit of work. All added/removed cache rows, their
  bounded lifecycle events, and a count-only `dataset_cache_reconciled` event
  commit together; direct cache CRUD remains compatible.
- The existing `cache_reconcile` JSONL summary remains as an explicit
  compatibility projection (default-on, suppressible through
  `LEGACY_AUDIT_JSONL_ENABLED`); it is not treated as authoritative. Dataset
  names/versions remain in row-level durable events; the reconcile event carries
  only server identity and add/remove counts.
- Empty observations are idempotent, and append-failure tests prove the whole
  cache map remains unchanged with no partial durable events.

## 2026-08-05 — Service identity approval decision UoW

- Approved service-account creation and service-token issue/revoke decisions
  now use one `BEGIN IMMEDIATE` unit of work for the actor/account or token
  mutation, the bounded `service_account_created`/`service_token_created`/
  `service_token_revoked` event, and `approval_decided`.
- The identity event carries the approval correlation but never bearer tokens,
  secret hashes, labels, or scope values; direct identity CRUD remains
  compatible and continues to emit its existing non-approval lifecycle event.
- Revalidation preserves service-account name/actor invariants, disabled-account
  rejection, and already-revoked idempotency. Fault-injection tests prove that
  an append failure leaves the identity mutation rolled back and the approval
  pending; successful create/issue/revoke paths assert event correlation.

## 2026-08-05 — Identity membership decision UoW

- Approved project-membership grant/update/remove decisions now use one
  `BEGIN IMMEDIATE` unit of work for the membership row, the
  `membership_granted`/`membership_revoked` durable event, and the
  `approval_decided` event. Direct membership CRUD remains compatible.
- The UoW correlates the identity event to the approval without storing role
  or credential material beyond the bounded project/actor identifiers and
  role; repeated same-role or already-absent decisions remain idempotent.
- Fault-injection tests prove both grant and remove leave the membership and
  approval pending when the durable append fails; successful paths assert the
  approval correlation.

## 2026-08-05 — P1-4 literal audit inventory gate

- Adoption catalog now explicitly inventories every one of the 55 literal
  `append_audit()` actions found under `app/` and `dispatch_center/`. The
  catalog has 64 typed entries, including 16 owner/roadmap-tracked legacy or
  operational compatibility entries; this does not upgrade those paths to
  durable evidence.
- `scripts/audit_adoption_gate.py` now fails closed for a new unclassified
  literal legacy action as well as for a durable action sent to the JSONL
  writer. Dynamic action values remain covered by domain tests and explicit
  ownership metadata.
- Added a direct gate regression test and updated the audit/roadmap/catalog
  documentation. No runtime behavior or legacy JSONL compatibility switch was
  changed.

## 2026-08-05 — P1-4 approval audit and runtime evidence slice

- Approval creation (`insert_approval` and pinned-contract creation) now writes
  an `approval_created` durable event in the same SQLite transaction as the
  pending row. Payload bytes are not copied into audit parameters; the event
  retains only safe kind/identity metadata and an approval correlation ID.
- Approval status transitions now write `approval_decided` atomically with the
  `approved`/`rejected` update. A durable append failure rolls the decision
  back to `pending`; repeat updates do not create duplicate decision events.
  Resource/approval correlation fields are preserved in API/export projections.
- `app.audit_adoption` now records approval create/decide/approve/reject as
  durable entries; there is no remaining approval `approve` compatibility
  writer. The generic `reject()` route no longer writes a duplicate JSONL line;
  its `approval_decided` event is the authoritative transaction-bound evidence.
  `scripts/audit_adoption_gate.py`
  is a required CI step that validates catalog ownership/tracking, rejects
  unclassified literal or closed-dynamic actions, and rejects literal durable
  actions sent to `append_audit()`. Pinned canary, worker
  validation, and execution-plan approval UoWs no longer emit duplicate
  legacy approval/plan summaries. Ordinary enqueue approvals now use a single
  UoW for the complete unpinned setup/sync/bundle Job graph, approval decision,
  and bounded `execution_job_materialized` events; graph failures roll back
  every Job. Dataset prewarm sync Jobs now use the same UoW and durable
  materialization event. Standalone enqueue still emits an explicit legacy
  JSONL compatibility line, while its Job materialization is durable; the
  policy-scoped auto-placement decision is now a transaction-bound durable
  event.
- The existing `approval_requested` JSONL request summary is explicitly
  catalogued as `approval.compatibility`; it remains separate from the
  transaction-bound `approval_created` and `approval_decided` events.
- Experiment-record create/update/delete writes from both HTTP and Agent-tool
  paths now share a transaction-bound durable event UoW. Events keep only
  bounded project/record identity, author/kind or changed-field metadata, and
  relationship-presence flags; content and titles are excluded. Fault-injection
  tests verify row/event rollback for all three mutations.
- The legacy queued-Job cancellation path now uses a dedicated status-CAS UoW
  and emits `execution_job_cancelled` without command text. Rollback coverage
  leaves the Job queued; Engineering Task owner Jobs still fail closed through
  the existing generic-cancel guard.
- Generic reconcile terminal done/failed, interrupted requeue, and dependency
  blocked transitions now use the optional Job UoW hook for bounded durable
  events. Rollback tests cover each path; log tails, commands, and raw
  dependency payloads remain outside the ledger. `requeue_blocked` remains an
  explicit compatibility error summary for approved-stop races.
- Audit export operational metrics now include durable outbox pending,
  processing, failed, exported, and dead-letter state counts plus the oldest
  processing lease age; no JSONL file state is inferred as success.
- `LEGACY_AUDIT_JSONL_ENABLED` is now a typed, default-on compatibility flag.
  Setting it to `false` retires only the already-durable standalone enqueue and
  unbound coding-task and unpinned-server compatibility summaries; durable
  database events and rejection/error evidence remain enabled. The feature
  report exposes its owner, review date, and external-export/retention
  retirement condition.
- Identity service-account/token, membership, and session lifecycle writes now
  append durable events in their same transaction; secret hashes and bearer
  material remain excluded. Service-account disable is covered as well.
- Node enrollment, credential rotation (including staged activation), drain,
  revocation, and routine retirement now append bounded durable lifecycle
  events in the same transaction as the Node mutation. Token values, secret
  digests, activation nonces, and credential identifiers are not persisted in
  audit parameters; rollback tests cover enrollment and revocation failures.
- Legacy Node attempt creation (direct insert and poll lease) now emits one
  transactional `node_attempt_created` event with only job/node identifiers and
  lease mode; reuse paths do not duplicate the event.
- Generic and legacy Node terminal convergence now emits one
  `execution_terminal_recorded` summary transactionally (state, exit code,
  job/node identifiers only); terminal retries do not duplicate it and log
  tails remain outside durable audit.
- Immutable Node artifact reports now emit one report-digest-bound
  `execution_artifact_recorded` summary per distinct report. Exact/reordered
  retries remain idempotent, and paths/content are excluded from durable
  parameters; rollback is covered.
- Generic stop outbox creation now emits a bounded
  `execution_stop_requested` event. Node stop request/ack transitions emit
  `execution_stop_requested`/`execution_stop_acknowledged` transactionally;
  the approval path commits its `approval_decided` and approved stop event
  with the pending operation, and append failures roll back the mutation.
- Uncertain launch settlement now emits `launch_resolution_recorded` for
  positive launcher evidence and controller-won non-transmission. Only proof
  and resolution categories are recorded; the attempt evidence and outbox
  transition roll back together if the durable append fails.
- Generic and Node completion collection now emit
  `execution_result_recorded` with bounded outcome metadata. Collection
  payloads stay in the immutable operation rows, while durable event IDs make
  delivered/failed retries idempotent.
- Coding-run creation now emits `run_created` in the same transaction with
  only bounded project/runner/binding metadata; instruction contents stay in
  the run row and append failures roll the creation back.
- Project create/update/delete, dataset registry creation, card updates, cache
  add/remove mutations, and sync verification now emit bounded durable project
  or dataset events. Project repository paths, dataset source paths, manifests,
  card text, and raw SSH/manifest diagnostics remain out of the ledger. Sync
  Job terminal status, successful cache registration, and
  `dataset_sync_verification_recorded` commit together; retries are idempotent
  and durable-audit failures roll the Job/cache mutation back. Archive is not
  a public operation yet; older registry compatibility remains explicit legacy.
  Snapshot build
  approval and publish/abort transitions add `approval_decided` and
  `project_snapshot_published` transactionally.
- Pinned server publication prepare, YAML transition, activation, and exact
  rollback/recovery now emit operation-specific durable events alongside the
  `approval_decided` event inside the publication transaction; the catalog
  tracks this as `server.publication`. The four server approval decisions are
  durable as well. Unpinned legacy YAML still cannot fabricate a revision, but
  now records hash-bound `server_legacy_mutation_intent` and applied/failed
  outcome events; only its old JSONL summary remains under the explicit
  `server.compatibility` entry.
- Execution-plan, Engineering Task, validation, and promotion approval
  creation/decision paths now use the same durable approval envelope. Task
  creation/queued/rejected updates, bound coding-run result projection, and
  project-version promotion prepare/finalize/retire emit bounded transactional
  events. Native Engineering Task retry and discard now also commit their
  approval decision, task projection, and route-specific durable event in one
  UoW; the bound result is catalogued as `engineering_task.result`. The
  unbound `coding_task` Job materializer is also transaction-bound; only its
  wrapper's legacy JSONL summary remains explicit compatibility. Native v1
  approvals no longer emit that legacy summary.
- Real user-systemd integration evidence now covers strict credential/control
  isolation, transient cgroup stop, Agent-store restart recovery, and applied
  memory/task resource properties. The test remains skip-safe on hosts without
  a user systemd manager.
- Run Profile and Dispatch Policy create/update/archive approvals now insert
  their immutable revision, bounded `*_revision_created` durable event, and
  typed `approval_decided` record in one SQLite UoW. Rollback tests inject an
  audit append failure and verify that neither the revision nor approval
  decision survives; the historical route-specific JSONL summaries remain
  explicit compatibility entries.

**Evidence**

- Durable audit, approval, actor-attribution, execution-foundation, service-
  token and API groups pass after the migration slice.
- Full offline regression suite after the Node lifecycle/attempt/terminal/
  artifact, launch/stop/result, run/project, snapshot, approval-envelope,
  Project create/update/delete, Dataset sync verification/server publication,
  generic reject migration, Engineering Task result/catalog, native retry/
  discard UoWs, approval catalog split, native coding-task compatibility split,
  server compatibility catalog split, pinned/plan/validation summary removal,
  supervisor stop-race, ordinary enqueue graph, validation/native Job
  materialization, and auto-placement policy UoW slices:
  candidate/import, canonical-version, project-deploy, and apply-patch
  remote-boundary slices (including `hub_sync` and `server_bootstrap`):
  `3582 passed` (`916.58s`, 0:15:16), 0 failed. This run includes the
  unbound coding-run result, scheduler stalled-state durable UoW rollback,
  ExecutionPlan materialization/append-rollback coverage, and the audit export
  alert projection checks.
- Real systemd integration: `4 passed`; Ruff and mypy pass; the adoption gate
  reports `status=ok` with `70` catalog entries (16 explicit legacy
  compatibility/inventory entries)
  (ordinary graph materialization, validation/native Job materialization,
  unbound coding-task Job materialization, dataset prewarm materialization,
  policy-scoped auto-placement decisions, and unpinned-server intent/outcome
  evidence are durable; the legacy enqueue/coding-task/server JSONL summaries
  remain explicit compatibility entries).
- The 2026-08-06 dynamic-writer and Node enrollment/rotation/retirement/
  revocation rollback additions are included in the full-suite count above;
  the focused Node/audit/actor/recovery group is `207 passed`.
- The legacy JSONL switch is covered in configuration, typed-settings,
  standalone-enqueue, and unbound-coding-task tests; its default preserves
  compatibility and its disabled path preserves the durable materialization
  event.
- Added the read-only `scripts/audit_export_status.py` gate. It reports the
  durable outbox's pending/failed/processing/dead-letter counts and returns a
  non-zero result under explicit `--require-clear`; it does not invent an
  alert threshold or claim continuous production monitoring. The status and
  `/operations/metrics` now also expose the shared critical dead-letter alert
  signal.
- This is local/offline evidence only. External immutable audit storage,
  production Node canary, and the remaining full-domain audit migration are
  still open gates.

## 2026-08-06 — Remote PR/CI handoff

- Commit `9d2d792` is pushed to `codex/wp2d-canary-7ecbec6`; PR #21 is now
  Ready for review with merge state `CLEAN`. Both push and pull-request
  required `python-tests` runs completed successfully after the two remote
  suite failures were repaired.
- Reviewer approval, unresolved-comment review, PR-size decomposition, 117
  canary/rollback, and production audit-anchor gates remain open; this entry
  records remote CI evidence only and does not claim merge or deployment.

## 2026-08-06 — Review wheel artifact CI

- Commit `d0304a5` adds an exact-commit `dispatch-wheels-<commit-sha>` upload
  after the package smoke gate. Both push and pull-request required
  `python-tests` runs completed successfully; the push artifact is retained for
  14 days and is suitable only for a non-production canary installation.
- The artifact has not been installed on a Node or 117 canary host, and this
  evidence does not claim canary, rollback, merge, or production readiness.

## 2026-08-04 — `worker_5090_117` Level B smoke (not canary)

- Manual approval `#110` materialized exactly one pinned Job (`61`) on the
  designated non-production SSH worker. Candidate commit was
  `0551f774d8ef22e8361e2df4c9d6656a797988fe` and the approved payload digest
  was `1ad7a0e742a3a216f087245475133422f5c6d19a2307893d798976b654ff92e0`.
- Attempt `b2fab5af-7010-494c-b8b2-0320a38908a5` reached `done` with exit code
  `0`; exactly one `prepare`, one transmitted `launch`, and one `collect`
  operation reached `delivered`.
- The control plane was restarted into rollback-safe mode with reconciliation
  and outbox enabled, but new claims and SSH launch disabled. Active attempts
  and pending/processing/uncertain operations were both `0` after completion.
- This is a single-job smoke only. It does not close `RB-LAUNCH-001`, does not
  count toward the `ssh-canary-evidence-v2` 20-job/8-hour window, and includes
  none of the required formal response-loss, in-window restart, or rollback
  drill evidence.

## 2026-08-04 — PR-09 versioned Node protocol contract (local evidence)

- Added explicit `2.0` protocol/version-header constants and a canonical
  capability list on both independently installable sides of the Node
  package. The control plane rejects an explicitly incompatible header with
  `426` while preserving the existing no-header in-process compatibility path.
- Added authenticated, read-only `POST /node-agent/probe` and
  `dispatch-node-agent --probe`. Probe responses contain only node identity,
  capability, assignment, and drain metadata; credentials are never echoed.
- Added cross-package client/server contract tests, including version-header
  emission, incompatible response handling, and the HTTP `426` gate.
- The read-only probe adds one intentional API operation; the OpenAPI contract
  is now `128` paths / `134` HTTP operations / `50` schemas with snapshot
  `5292c9f938383555d84ff233184f4d2e3da9d494fe1c18cd626f834c10f14280`.
- Local focused Node Agent suite: `122 passed`. No node assignment flags,
  production endpoint, credential, or canary state was changed.

## 2026-08-04 — PR-10/11/12 local completion slices

- PR-10 adds an opt-in workload isolation contract in `agent/isolation.py`:
  strict environment allowlisting, separate transient systemd attempt-unit
  argv, read-only control-evidence mount, and bounded memory/CPU/task policy.
  The existing direct supervisor remains the explicit compatibility/rollback
  path; the template opts into strict environment mode only when installed.
  Local isolation and supervisor evidence: `17 passed`.
- PR-11 adds the additive `worker` process role and `dispatch-worker` entry
  point. Worker readiness owns the durable execution shadow/leader/outbox and
  result-recovery loops; scheduler readiness owns monitor/scheduling and
  maintenance loops; API starts none. The SQLite lease/outbox fencing remains
  the single contender guard. CLI, health, packaging, and role tests pass.
- PR-12 adds a dependency-free static frontend smoke/build gate plus Node
  syntax check in CI. Existing Run Wizard, Approval Inbox, Project timeline,
  project workspace, and capability presentation remain on the checked-in
  assets without introducing a second frontend runtime. Frontend focused
  tests: `33 passed`; `scripts/frontend_smoke.py` and `node --check` pass.
- These are local implementation/evidence slices only. No systemd unit was
  installed, no worker/node was contacted, and SSH remains the default backend.
- Final offline verification after the CI workflow-contract repair: `3443
  passed` in `638.45s`; Ruff, mypy, compile, diff, Node/frontend smoke, and
  wheel-boundary checks pass. The coverage gate remains above its 35% threshold
  (`1009 passed`, `38.74%`).

## 2026-08-04 — PR-08 verification gate repair

- Updated the packaging boundary contract to include the PR-07
  `dispatch_center.infrastructure` and `.db` packages already declared by the
  Control Plane wheel metadata.
- Packaging metadata and wheel-boundary tests pass (`7 passed`); the complete
  offline suite passes twice (`3428 passed` each run, `641.35s` and `635.81s`).
  The quickstart cleanup gate also passes ten consecutive runs (`39 passed` per
  run) with no timeout or orphan process.

## 2026-08-03 — Architecture refactor PR-08 durable audit/export outbox foundation

**Review status:** durable audit ledger/export foundation is complete for
selected UoW execution paths, but adoption remains **partial**. Legacy domain
mutations still use best-effort JSONL; full-domain migration and external chain
anchoring remain open.

- Added schema migration 2 with append-only `audit_events` rows, a per-event
  JSONL export outbox, and a SHA-256 predecessor chain. Existing JSONL history
  is not backfilled, so no actor/resource/timestamp provenance is fabricated.
- Added migration 3 with an explicit hash contract version, bounded
  allow-by-shape parameters, atomic outbox claim/retry-ceiling handling, and
  operator-only dead-letter replay. API rows expose durable/legacy evidence
  quality and the machine-readable partial-adoption catalog.
- Added cursor-bound durable audit append, retry/lease/dead-letter export
  operations, hash-chain verification during backup restore checks, and
  bounded `/events`/`/audit` reads. Legacy `append_audit()` remains
  best-effort and append-only; the DB ledger is now the source for migrated
  UoW writes.
- The v2 Node and attempt-driven SSH claim paths append their creation audit
  event in the same UoW transaction. A transaction-local cursor lets the
  existing tested facade join that boundary without changing its standalone
  atomic behavior.

**Evidence**

- Durable audit/export/migration/UoW/CLI plus API-evidence group: `32 passed`;
  OpenAPI/API,
  execution, audit/health, inventory/diagnose, and DB/CLI regression groups
  pass (`122`, `100`, and `54` tests in the recorded runs).
- Coverage gate: `1006 passed`, total coverage `38.89%`, threshold `35%`.
- Ruff, full mypy (112 source files), OpenAPI snapshot, compile, diff, and
  static invariant checks pass. Rebuilt Control Plane and Node Agent wheels
  both pass the boundary check.
- Backup/restore verification validates SQLite integrity and the durable audit
  hash chain. Export write failures leave the DB event intact and can reach a
  bounded dead-letter state; replay is explicit and audited. The chain is an
  internal-consistency check only: external off-host anchoring and full-domain
  adoption remain open production gates.

## 2026-08-03 — Architecture refactor PR-07 repository/UoW seam

- Added typed Node/Execution repository protocols and SQLite adapters under
  `dispatch_center.infrastructure.db`. The adapters delegate to the existing
  atomic facade methods, so Node claim, acknowledge, terminal, artifact, and
  stop invariants remain unchanged while callers gain an explicit boundary.
- Added `SQLiteUnitOfWork.run()` and `Database.transaction()` for future
  multi-repository commits with fail-closed nested-transaction detection and
  rollback fault injection. Node protocol writes and the v2 Node/SSH attempt
  creation call sites now enter through the UoW compatibility seam.
- This is intentionally additive: artifact resend overwrite semantics and the
  legacy `Database` facade remain unchanged until the separately reviewed
  artifact-immutability work package.

**Evidence**

- Node/Execution/UoW focused group: `176 passed`.
- Ruff, mypy (6 changed source modules), compile, and diff checks pass. The
  repository-wide suite still needs an idle-host rerun because its four
  quickstart cleanup timeouts were environmental, not UoW failures.

## 2026-08-03 — Architecture refactor PR-06 versioned DB migration

- Added an SQLite-native, append-only migration runner with a checked-in
  migration ledger (`schema_migrations`), synchronized `PRAGMA user_version`,
  deterministic migration metadata checksums, process lock, and one
  transaction per upgrade plan. Failed migration callbacks roll back both DDL/
  data and their ledger row; no destructive down migration is provided.
- Moved the existing additive legacy-column/backfill/index work behind the
  version-1 `legacy_schema_compatibility` migration. The compatibility
  `Database(path)` constructor still auto-upgrades for existing local/test
  callers, while deployments can run the explicit `dispatch db upgrade`
  preflight before application rollout.
- Added read-only `dispatch db current`, `upgrade`, `check`, `backup`, and
  `restore-verify` commands plus SQLite online-backup and integrity helpers.
  `schema_is_initialized()` now fails closed when the migration ledger is
  absent or behind the checked-in target.
- Added migration failure-injection, idempotence, backup/restore, CLI, and
  unknown-ledger metadata tests. Existing SSH execution and all assignment
  defaults remain unchanged; no production server or credential was touched.

**Evidence**

- Migration/database/health focused group: `51 passed`.
- Coverage gate: `991 passed`, total coverage `38.07%`, threshold `35%`.
- Complete offline suite: `3408 passed, 4 failed in 835.48s`; all four failures
  are existing `tests/test_quickstart_script.py` cleanup timeouts while the
  shared host was saturated by unrelated training processes (load average
  ~24.7). They are `subprocess.wait()` timeouts for deliberately sleeping test
  children, not migration assertions; rerun on an idle host is required before
  calling the repository-wide gate fully green.
- Coverage gate: `991 passed`, total coverage `38.07%`, threshold `35%`.
- Migration/database/health focused group: `51 passed`; Ruff, mypy (101 source
  files), compile, diff, static invariant checks, and rebuilt Control Plane /
  Node Agent wheel-boundary checks pass.

## 2026-08-03 — Architecture refactor PR-05 authorization enforcement

- `AUTHORIZATION_MODE` now accepts `off`, `shadow`, and explicit `enforce`.
  The compatibility default remains `off`; the existing shadow observer remains
  fail-open and the new `app.authorization_enforce` adapter is the only
  fail-closed integration.
- Enforce mode applies the closed route-action catalog to HTTP interfaces and
  local agent tools, resolves resources to project/global scope, requires exact
  service-token scopes, blocks legacy shared-token global administration, and
  applies high-risk approval separation of duties.
- Supported project-scoped collection routes filter durable rows before
  serialization (`projects`, `jobs`, `datasets`, snapshots, approvals,
  engineering tasks, and coding runs). Existing feature-gate 404 responses and
  the public static mount remain unchanged in enforce mode.
- Added stable API error coverage for enforcement denials, cross-project and
  service-scope tests, legacy-token compatibility tests, local-tool scope tests,
  and WebSocket enforcement. The engineering-task list handler retains its
  historical direct-call compatibility for non-HTTP tests.

**Evidence**

- Complete offline suite: `3406 passed in 796.15s`.
- Coverage gate: `992 passed`, total coverage `39.93%`, threshold `35%`.
- Focused authorization/API regression group: `510 passed`; Ruff, mypy (`95`
  source files), compile, diff, and static invariant checks pass.
- Rebuilt Control Plane and Node Agent wheels in a temporary directory;
  wheel-boundary check passes. No production server, credential, worker,
  external provider, or SSH target was contacted; SSH remains the existing
  execution backend and all assignment/default rollout flags remain unchanged.
- This is not a claim of hostile multi-tenant or worker filesystem isolation;
  those require the later isolation, protocol, and canary evidence in PR-09/10.

## 2026-08-03 — Architecture refactor PR-04 API boundary

- The compatibility API now registers its existing 133 HTTP operations and
  `/ws` through a fixed twelve-router registry under
  `dispatch_center.api.routers`; route paths, methods, operation IDs, schemas,
  and authorization metadata remain unchanged.
- Pydantic request schemas live in `dispatch_center.api.schemas`.  The legacy
  imports from `app.main` remain as compatibility aliases while handler bodies
  still use the existing application state; later use-case/service work will
  remove that remaining monolith dependency.
- Added an additive `APIError` envelope and server-generated UUID4
  `X-Request-ID`.  Legacy `detail` responses remain unchanged until each use
  case is explicitly migrated.  Early authentication and 404 responses also
  receive the correlation header.
- Added route-ownership and API-foundation tests plus
  `docs/API_ROUTING.md`.  The OpenAPI snapshot remains the pre-extraction
  hash: 127 paths, 133 HTTP operations, and 50 schemas.

**Evidence**

- Complete offline suite: `3399 passed in 813.83s`.
- Coverage gate: `985 passed`, total coverage `38.11%`, threshold `35%`.
- Ruff, mypy (`94` source files), compile, static invariant checks, and rebuilt
  Control Plane/Node Agent wheel-boundary checks all pass.
- No production server, credential, worker, or external provider was
  contacted; SSH remains the existing execution backend and all rollout flags
  retain their prior defaults.

## 2026-08-02 — WP-2D restart finding and same-state observation repair

- A fresh formal window on candidate `6df6771844f5712744cc35f6f5b51721e7350bfd`
  started with Job `#36`. The control-plane restart correctly advanced the
  scheduler fencing epoch from 5 to 6, retained exactly one SSH attempt and
  never requeued or relaunched the workload. The remote workload later reached
  `done` with exit code 0, and prepare/launch/collect each had exactly one
  delivered operation.
- The restart drill nevertheless failed: repeated positive tmux observations
  of the already-`running/known` attempt were sent through the lifecycle graph
  as `running -> running`. The DB correctly rejected that self-transition, and
  the scheduler logged `ValueError: invalid execution attempt transition`
  until the terminal sentinel appeared. Failed evidence is retained under
  `.runtime/evidence/wp2d-v2-20260802-6df6771`; this candidate is not
  canary-proven.
- Reconcile now treats matching same-state evidence as an observation-freshness
  update, fenced by the current leader lease and exact prior state/liveness. It
  does not append a fabricated transition. If a prior unreachable observation
  left the same running attempt `unknown`, matching positive evidence restores
  only liveness to `known` through the existing CAS transition path.
- Two direct regression tests cover idempotent post-restart
  `running/known -> running/known` observation and
  `running/unknown -> running/known` recovery. Focused attempt/foundation/
  launch-arbitration evidence is `130 passed`; requirements lock, compile,
  static invariant and diff checks pass.
- Full external-network-denied suite: `3355 passed in 639.86s`. New SSH launch
  assignment has been disabled in `.env`; reconcile/outbox remain enabled.
  Because the repair changes candidate runtime, WP-2D must start a new exact
  8-hour window after this change is committed and loaded.

## 2026-08-02 — WP-2D forced-response-loss finding and convergence repair

- The first forced-response-loss drill on candidate `74f76aae...` correctly
  kept Job `#35` on its single attempt: launch became `uncertain`, liveness
  became `unknown`, matching remote tmux evidence restored `running/known`,
  and the workload later reached `done` with exit code 0. No second attempt or
  launch was created.
- The drill nevertheless failed its formal close criterion: the reconciler
  restored the attempt but left the original launch operation permanently
  `uncertain`. The pre-fix evidence is retained as a failed observation under
  `.runtime/evidence/wp2d-v2-20260801`; the candidate is not described as
  canary-proven.
- Reconciliation now requires exact companion attempt/fencing identity before
  trusting tmux, sentinel or receipt evidence. A canonical receipt is checked
  against its exact attempt, fencing token and expected session; only sanitized
  digest/boot/contract metadata is persisted.
- Matching positive evidence settles the same launch operation
  `uncertain -> delivered` with `transmission_state=transmitted` and never
  replays launch. A controller-won claim atomically settles it
  `uncertain -> failed` with `not_transmitted` before the attempt may requeue.
  DB guards refuse settlement without persisted authoritative evidence.
- Upgrade recovery also inspects terminal SSH attempts which inherited an
  uncertain launch from older code. This path only repairs operation evidence;
  it never reopens the attempt, invokes the launcher, repeats collection, or
  repeats terminal hooks.
- The runbook now uses `scripts/wp2d_response_loss_worker.sh`, because observed
  launch acknowledgement latency was too short for a human to insert a
  firewall rule reliably after approval. The helper is bounded to one explicit
  non-production Job and automatically restores the rule.
- Focused attempt/scheduler/canary group: `197 passed`.
- Full offline release suite: `3353 passed in 639.55s` plus requirements lock,
  compile, static invariant and diff checks all passing.
- Runtime behavior is unchanged until the service is restarted on the new
  commit. Because this changes the candidate runtime, the formal 8-hour WP-2D
  window must restart; the earlier 19 successes and failed drill remain
  diagnostic history, not evidence for the new candidate.

## 2026-08-02 — WP-2D canary request seam

- The first live canary submissions exposed an honest integration gap: the
  ordinary web dispatch route creates legacy-compatible enqueue approvals, so
  Jobs `#14` and `#15` correctly received no generic execution attempt. Both
  were cancelled while still queued; neither reached SSH.
- Added `scripts/request_wp2d_canary.py`, an operator-only request tool for the
  already-approved `enqueue-execution-v1` contract. It requires an explicit
  non-production acknowledgement and an active approved SSH revision with
  revision-scoped D-5 `eligible` evidence. Request creation writes only a
  pending approval and a redacted digest audit event; it performs no SSH and
  creates no Job.
- Manual approval revalidates the same exact revision and atomically
  materializes one pinned Job through the existing WP-1B publication
  transaction. Web-direct, service-token and auto-rule decisions are refused.
  Ordinary `/dispatch` behavior remains unchanged.
- This closes only the missing canary submission seam. It is not canary
  evidence and does not change `RB-LAUNCH-001`: the 8-hour / 20-attempt window
  still starts only when the first valid execution attempt is persisted.
- The first successful smoke attempt also exposed an evidence-boundary bug:
  runtime timestamps use ISO `+00:00`, while the v2 manifest requires UTC `Z`.
  Raw SQLite text comparison omitted an attempt exactly equal to `since`.
  `canary_report.py` now compares all scoped attempt/event timestamps as
  SQLite instants (`julianday`) and has a regression test for equivalent UTC
  encodings. The smoke attempt remains historical evidence; the formal window
  is reset only after the fixed evaluator is committed and loaded.

> 對應計畫：`docs/NEXT_IMPLEMENTATION_PLAN.md`
>
> 規則：只有具備程式、測試及驗證證據的項目才標 `completed`；尚缺證據的
> 項目維持 `in_progress`，不以「已建立檔案」冒充完成。
>
> 本文件下方的日期區段保留每次 handoff 的歷史快照；若歷史段落與本文件
> 最上方的 2026-08-01 runtime continuation 不同，以下方歷史為「當時狀態」
> 讀取，現在能力以最上方、`CAPABILITY_LEDGER.md` 與 `TESTING_REPORT.md`
> 為準。

## 2026-08-01 — WP-2D v2 eight-hour canary amendment

- User-approved `DG-WP2D-CANARY-v2` changes only the WP-2D observation window
  from 24 hours to 8 hours. The 20-terminal-workload minimum, three required
  drills and every zero-error/100%-collection criterion remain unchanged.
- `scripts/canary_report.py` now pins `ssh-canary-evidence-v2`, rejects v1
  manifests and fails a 7:59:59 window. Historical v1 decision text remains
  intact; Phase 5 Node and Phase 6 RPO durations are unaffected.
- Focused evaluator/document-authority group: `15 passed`.
- Full offline release suite: `3338 passed in 651.04s`.

## 2026-08-01 — D-5 SSH attempt filesystem preflight closure

### Revision-scoped fail-closed preflight — `completed (local evidence)`

- Added one fixed, read-only remote command that runs `stat -f` against
  `agent_jobs`, or the login home when that directory does not yet exist. It
  accepts no caller-controlled command/path bytes and creates nothing.
- `POST /server-config/{name}/attempt-preflight` requires an active approved
  SSH revision whose exact target and key-file identity still match. It
  classifies a bounded filesystem type, records status/timestamp/contract/type
  on that revision and revalidates revision/credential identity after the SSH
  round trip before the CAS write.
- Known local filesystems record `eligible`; NFS/CIFS/FUSE and other known
  distributed filesystems record `ineligible_non_local_fs`; transport errors,
  malformed output and unclassified types record `unknown`. No exception text
  is returned or audited.
- Every new server revision starts with NULL evidence. The scheduler revision
  map requires positive `eligible` evidence for backend=ssh, and
  `create_execution_attempt()` independently rechecks it inside the claim
  transaction. Backend=node does not use the SSH `agent_jobs` mkdir launcher
  and remains outside this SSH-specific gate.
- The Worker UI exposes revision/preflight status and the operator action. The
  route is classified `PLATFORM_MANAGE`; the observation does not edit
  `servers.yaml`, reveal credential bytes or enable any rollout flag.
- Existing legacy entries can now be explicitly re-approved through the
  existing `server_update` kind with an exact no-op update. Approval creates
  revision 1 only when that server has no revision history; a missing active
  row with retired/prepared history fails closed instead of being resurrected.
  The Worker UI exposes this as **建立受管 Revision**. No approval kind or
  mutation route was added, matching the approved RB-SERVER addendum.

**Evidence**

- Focused parser/API/schema/migration/claim/UI group: `144 passed, 1 warning`.
- Adoption/publication/API/attempt/auth/UI regression group:
  `235 passed, 1 warning`.
- Full offline release suite: `3337 passed, 1 warning in 681.63s`.
- Exact lock, static invariant gate, compile and diff checks pass. No real
  worker was contacted and `worker_5090_117` still has no fabricated revision
  or filesystem observation.

## 2026-07-30 — Runtime integration continuation

### Phase 4 Node v2 runtime contract closure — `completed (local evidence)`

- Strict Node assignment now creates one generic `ExecutionAttempt` and one
  linked protocol attempt in the same lease transaction. Ack atomically
  changes the linked attempt to dispatching and the canonical Job to running;
  terminal is first-writer-wins across the generic attempt, protocol attempt
  and Job. The SSH reconciler excludes Node-owned attempts.
- Heartbeat expiry is a fenced liveness transition from known to `unknown`.
  It does not mark the Job failed, requeue it or authorize SSH fallback; a
  matching heartbeat restores known liveness.
- Daemon reconnect and ack-response-loss recovery require exact attempt,
  payload digest and durable local `not_launched` evidence. Credential
  revision validation includes the exact key-file identity, so a repointed
  server or key fails before material result/outbox work.
- Every accepted Node terminal atomically appends exactly four immutable
  completion operations: `dependency_refresh`, `result_collection`,
  `notification` and `owner_projection`. A fenced worker claims the four as a
  bundle, persists delivered/failed outcomes and recovers pending or expired
  claims after control-plane restart.
- `scripts/node_canary_report.py` now validates the exact candidate revision,
  server/Node/agent/credential bindings, all acknowledged workloads reaching
  terminal, and all four completion operations being delivered. Its strict
  evidence manifest and seven-day/two-Node/100-workload thresholds remain
  impossible to satisfy with local tests alone.

**Evidence**

- Node protocol/daemon/lease/credential/canary focused group:
  `281 passed`; the tests include duplicate terminal, stale-claim fencing,
  restart recovery, reconnect response loss, exact target/key drift and
  fail-closed canary evaluation.
- No Node was enrolled, no service was installed and all Node rollout flags
  remain default-off. Phase 5 and `DG-NODE-CANARY` are still open.

### WP-2D and Phase 5 evidence evaluators — `completed (local tooling)`

- `scripts/canary_report.py` now enforces the approved
  `ssh-canary-evidence-v2`: an explicit 8-hour UTC window, exact
  non-production server/candidate evidence, at least 20 terminal SSH attempts,
  exactly one delivered collection per terminal and the three required drills.
  Missing work or malformed evidence cannot produce a pass, and JSON output
  preserves the verdict exit code.
- `scripts/node_canary_report.py` and
  `docs/PHASE5_NODE_CANARY_RUNBOOK.md` provide the corresponding strict Node
  evaluator and operator procedure. These tools make the future gate
  reproducible; they do not count as canary evidence.

### WP-6A gate-independent operations — `completed (local evidence)`

- `PROCESS_ROLE=all|api|scheduler` now makes topology explicit. The compatible
  default remains one `all` process; `api` starts no scheduler/monitor/
  maintenance task. When durable execution ownership is enabled, a contender
  without the live fencing epoch skips the entire scheduler tick, including
  the legacy SSH branch.
- Background tasks are named and supervised. Unexpected clean/exception exits
  make readiness fail and are exposed without exception text. Critical loops
  publish tick/error counts, safe error categories and per-loop freshness.
- Authenticated `GET /operations/metrics` combines durable execution queue,
  attempt/outbox/collection state, Node liveness/unknown attention counts,
  process role/leader state, SQLite/filesystem capacity and backup age. Backup
  age is observable but its threshold is `null`: an unapproved draft has not
  silently become policy.
- `deploy/backup.sh` refuses a missing database, creates one exclusive
  destination per invocation, writes per-artifact SHA-256 inventory and pins
  that inventory in `MANIFEST`. Archives are rejected for traversal,
  absolute paths, links, devices or duplicate members before extraction.
  Restore validates and stages the entire backup before any prompt or
  displacement. The systemd oneshot fails closed without an explicit
  `BACKUP_ROOT`; the daily timer is a disabled draft template.
- `docs/PHASE6_OPERATIONS_RUNBOOK.md` records topology, takeover, metrics,
  off-host backup and restore-drill procedures, including the exact boundaries
  that still need real infrastructure or `DG-OPS-SLO`.

**Evidence**

- Phase 6/config/auth focused group: `95 passed, 1 warning`; expanded
  execution/foundation/document group: `149 passed, 1 warning`.
- Tampered/malicious archives, a missing source database and a missing
  automated-backup destination all fail before current state is touched;
  same-second backup invocations cannot merge.
- This is local code/runbook evidence only. No unit was installed or enabled,
  no off-host backup was created, and no real takeover/restore drill was run.

### Public server publication, native code promotion and Run lineage — `completed (local evidence)`

- Public server add/update/disable/delete requests now pin the exact canonical
  post-mutation YAML bytes and before/after digests in `server-config-v1`.
  Approval writes the pinned document, verifies the observed digest, reloads
  runtime state and only then activates the revision. Exact compensation is
  used on reload failure; drift or an unknowable disk state remains in the
  durable journal for operator recovery.
- `DG-CODE-PROMOTE-v1` is now a real native Engineering Task workflow rather
  than a database-only placeholder. The default-off request route/UI resolves a
  terminal, non-discarded task, its pinned base ProjectVersion, canonical
  `results/{job_id}/changes.bundle` and verified artifact digest. Manual
  approval repeats those checks, runs real Git bundle verification in an
  isolated bare repository, prepares a non-runnable ProjectVersion, publishes
  `refs/heads/codex-promoted/{version_id}` in the local Hub, verifies the ref,
  then atomically makes the version runnable. Publication interruption keeps
  the same row pending/non-runnable for idempotent retry; retirement never
  deletes evidence.
- ProjectVersion promotion provenance is protected by additive columns,
  approval/reference checks and immutable/delete-restrict triggers. Historical
  rows remain `legacy_observed`; no migration invents promotion history.
- A Run request now creates its immutable ExecutionPlan and pinned pending
  approval in one SQLite transaction, with `request_approval_id` set at plan
  creation and protected from later rewriting. Injected failure after approval
  insertion rolls both rows back.
- `GET /runs/{plan_id}` now returns the durable plan → approval → Job →
  execution attempts → operations/events → linked Node protocol/artifact
  metadata graph. Missing attempts/artifacts are returned as empty evidence,
  not interpreted as failure; Node artifacts are explicitly metadata-only.

**Evidence**

- Real Git promotion tests create a working repository, prerequisite bundle
  and bare Hub without network access. They cover flag-off refusal, exact
  pinned payload, tampered/regenerated bytes, corrupt bundle verification,
  missing base, manual-only approval, publish interruption/retry, duplicate
  promotion no-op, immutable provenance, retirement and planner eligibility.
- Promotion/config/auth/UI focused group: `323 passed, 1 warning`.
- ExecutionPlan/attempt foundation group: `69 passed, 1 warning`.
- Phase 6 role/fencing/metrics/backup/config/auth focused group:
  `95 passed, 1 warning`.
- Full offline release suite:
  `3337 passed, 1 warning in 681.63s`; the warning is the existing
  Starlette/httpx TestClient deprecation.
- Exact requirements lock, static invariant gate, `compileall` and
  `git diff --check` all pass.
- No production server, SSH worker, external service or production credential
  was contacted. All rollout flags remain default-off.

### WP-2C/3B integration hardening — `completed (local evidence)`

- `plan_run` materialization validates the immutable `execution-plan-v1`
  payload and commits Job creation plus approval publication in one SQLite
  transaction. A queued pinned Job whose approval is still pending is not
  dispatchable.
- Plan-derived attempts revalidate plan/job/target identity and authorize only
  `prepare`, `launch`, and `collect`; stop remains a separate stop-intent
  approval.
- Attempt claim and the first `prepare` outbox intent are one DB transaction.
  The scheduler excludes active generic attempts from legacy reconcile/dispatch.
  An owner-only outbox worker claims pinned operations, records
  `effect_started_at` before SSH, and leaves uncertain effects for evidence-led
  reconciliation.
- Node polling no longer sends `job_id`; restart recovery asks for the current
  attempt, acknowledged work is never relaunched, child exits are observed, and
  terminal reports are durably retried. The local journal fsyncs
  `not_launched`/launch intent, transport failures use bounded backoff+jitter,
  and stop delivery records a receipt before signalling an isolated process
  group. Protocol/drain and new-assignment flags are separate; routine rotation
  supports bounded overlap while legacy aggregate-only callers retain emergency
  hard-cut behavior.
- Readiness no longer reports green when no supervised loop has completed.
- Generic attempt terminal reconciliation now enqueues and delivers a separately
  authorized `collect` operation before invoking the existing result pull and
  notification hook. Public stop approval for an active generic attempt likewise
  publishes a pinned stop operation without performing SSH in the HTTP handler.

**Focused evidence**

- `304 passed, 1 warning` across the plan, attempt dispatch, scheduler, jobqueue,
  Node client/daemon, config and health suites (the broader Node evidence run is
  recorded below).
- Historical baseline before the public publication/promotion/lineage
  continuation: `3206 passed, 1 warning in 651.00s`. The current result is
  recorded in the section above.
- `python -m compileall -q app agent` passed.
- No remote worker, production credential, or external service was contacted.

### Node Agent crash-window hardening — `completed (local evidence)`

- The agent now fsyncs an attempt identity/digest journal before sending the
  remote acknowledge request. A lost acknowledge response remains explicitly
  unknown and cannot be relaunched from lease expiry alone.
- Command materialization verifies the immutable SHA-256 and is fsynced only
  after acknowledge. A launch intent is fsynced immediately before `Popen`;
  `shell=False`/new-session process isolation is explicit, and restart recovery
  will never repeat an attempt once that intent exists.
- `current-attempt` now returns the exact command payload needed to continue a
  *first* launch after response loss. Continuation requires matching attempt,
  job, digest, ack state, and a local journal proving no launch intent/pid.
- Focused Node client/daemon suite: `155 passed, 1 warning`; no real process or
  network was used (all spawns/transports are injected). Terminal evidence now
  persists a bounded log tail and an explicit, validated artifact manifest;
  artifact metadata delivery is retried independently of terminal delivery.

**Still open**

- `WP-2D` non-production SSH canary (8h v2, forced response-loss, restart and
  rollback).
- `DG-NODE-CANARY` and Phase 5 (two nodes, 100 jobs, seven consecutive days).
- `DG-OPS-SLO` decision and production-ready evidence.

### WP-3A snapshot workflow — `completed (local evidence)`

- Added the default-off `dataset_snapshot_build` approval contract and the
  request/approve/build/publish path. The request pins the registered source
  path, candidate digest, shard policy, store revision and byte ceiling; the
  approval transaction creates the immutable `building` row and publishes the
  approval attribution atomically.
- Local ArtifactStore publication now records manifest/descriptor paths, shard
  digests and byte/file counts in one immutable database transition. Source
  drift becomes `aborted`; unreadable state becomes `verification_unknown`.
- Added read-only snapshot listing/detail routes. No object-store, retention
  deletion, flag activation, deployment or real-data evidence is claimed.
- DB fencing now allows only one active/published winner for an identical
  dataset candidate; a control-plane crash can be resumed through the explicit
  `POST /dataset-snapshots/{snapshot_id}/resume` operator route using the same
  approved payload.
- Focused evidence: `tests/test_dataset_snapshot.py` passes 25 tests,
  including concurrent approval and interrupted-build resume cases. No
  object-store, retention deletion, flag activation, deployment or real-data
  evidence is claimed.

## 2026-07-27

### WP-0A-1 — TestClient / shutdown baseline

**Status:** `completed`

**Outcome**

- 分離出 Codex filesystem sandbox 的 AnyIO blocking-portal 限制：空白
  FastAPI TestClient 在沙箱內會停在進入點，但在一般本機環境能正常進出。
- 找到並修正真正的 application shutdown race：lifespan 取消
  `monitor_loop` 時，executor thread 仍在操作 SQLite，主執行緒卻已關閉同一
  connection，曾造成 Python segmentation fault。
- `AppState` 現在追蹤並 shield blocking executor futures，shutdown 會先排空
  它們再關閉共用資源；`Database.close()` 同時取得 connection lock 作最後
  防線。

**Changed**

- `app/main.py`
- `app/db.py`
- `tests/test_main_background_hooks.py`
- `scripts/testclient_smoke.py`

**Evidence**

- `timeout 20s .venv/bin/python scripts/testclient_smoke.py`
  → `TestClient lifecycle smoke: PASS`
- shutdown regression
  `test_stop_background_tasks_drains_blocking_db_call_before_close`
  → PASS
- 修正後完整測試不再於約 4% 發生 SQLite segmentation fault。

**Safety**

- 全部使用 in-process FastAPI、temporary SQLite 與本機 thread。
- 未連 production server、SSH、OIDC provider、LLM 或外部服務。

### WP-0A-2 — Deterministic offline pytest configuration

**Status:** `completed`

**Outcome**

- 新增 `pyproject.toml`，固定 test discovery、pytest-asyncio mode/loop scope
  與 faulthandler timeout。
- repository suite 預設拒絕 external socket/DNS，只允許 Unix/loopback；
  CI 再明確設定 `DISPATCH_TEST_NETWORK=deny`。
- network guard 的 boundary tests 不需要真的建立 AF_INET socket，因此在更
  嚴格的 filesystem sandbox 也能執行。
- server-update approval 測試改用 `tmp_path` 內 mode `0600` 的假 SSH key，
  不再依賴開發者家目錄，且沒有放寬 production key validation。

**Changed**

- `pyproject.toml`
- `tests/conftest.py`
- `tests/test_test_environment.py`
- `tests/test_agent_tools.py`
- `README.md`

**Evidence**

- `DISPATCH_TEST_NETWORK=deny ... pytest ...`
  → 62 passed
- `python -m py_compile ...` → PASS
- `git diff --check` → PASS

### WP-0A-3 — Clean current-environment suite baseline

**Status:** `completed`

**Outcome**

- 修正 server reload cache maintenance 意外擴大的 API response；移除已刪節點
  的 in-memory state 行為保留，公開 response 恢復既有 `{"ok": true}`。
- subprocess timeout 現在會在 kill 後排空 stdout/stderr transports，避免
  cancelled `communicate()` 在 event loop 關閉後才由 GC 清理。
- pytest parametrization 改為先 materialize cases，消除 pytest 10 相容性
  警告。
- 以三個明確、不重疊的 test-file groups 跑完 repository 目前全部 2892 tests，
  避免單次工具 300 秒上限把部分通過誤報成完整綠燈。

**Evidence**

- Group 1: 1226 passed
- Group 2: 828 passed
- Group 3: 838 passed
- Total: **2892 passed, 0 failed**
- `bash .claude/skills/release-gate/scripts/static_checks.sh`
  → PASS
- `python -m pip check` → no broken requirements
- `tests/test_requirements_lock.py tests/test_localrun.py
  tests/test_authorization.py tests/test_test_environment.py`
  → **161 passed**
- 加入 lock-sync tests 後完整 test discovery
  → **2896 tests collected**

**Historical warning status**

- Current local environment emits Starlette's TestClient/httpx deprecation
  warning because that既有環境尚未安裝新 lock 中的 `httpx2`。
- 原先的 pytest parametrization 與 asyncio subprocess finalizer warnings
  已由上述聚焦測試確認修正；exact-lock 環境的完整 suite 為 0 warning。
  Exact-lock CI 仍是 locked dependency combination 的最終依據。

### WP-0A-4 — Exact dependency lock and CI

**Status:** `completed`

**Implemented**

- `requirements.txt` remains the top-level dependency manifest.
- Added `httpx2>=2.9,<3.0` as Starlette 1.3+ TestClient's test backend while
  retaining `httpx` for application HTTP clients.
- Regenerated the Python 3.10 SHA-256 `requirements.lock`; the current lock
  includes exact `httpx2`, `httpcore2`, and `truststore` pins.
- Added `scripts/check_requirements_lock.py` plus boundary tests. CI now fails
  when a direct manifest dependency is missing, not exact-pinned, or outside
  its declared version constraint.
- Added bounded TestClient smoke and GitHub Actions flow:
  exact hash install → manifest/lock sync → smoke → collect → static gate →
  migration/core → complete suite.
- CI clears credential/provider settings and runs the suite with external
  network denied.
- Release dependency drift remains fail-closed: only the exact approved
  Authlib and httpx2 specs may differ from the current Git baseline.

**Evidence**

- `python scripts/check_requirements_lock.py`
  → `PASS (11 direct requirements)`
- `pip-sync --dry-run requirements.lock`
  → lock parsed successfully and produced a complete exact install plan
- static invariant gate → PASS
- CI workflow YAML parse → PASS
- `git diff --check` → PASS
- Fresh Python `3.10.12` venv:
  `/tmp/dispatch-center-wp0a-final-rdCbk3/venv`
- Lock SHA-256:
  `d74395fac8cdec0c332939d1736213bdb60123661414d63b762ba0fb372aa167`
- `pip install --require-hashes -r requirements.lock` → PASS
- `python -m pip check` → `No broken requirements found`
- Locked versions include FastAPI `0.140.0`, Starlette `1.3.1`,
  httpx `0.28.1`, and httpx2 `2.9.1`.
- `timeout 20s ... scripts/testclient_smoke.py`
  → `TestClient lifecycle smoke: PASS`
- Locked test discovery → `2896 tests collected`
- Locked complete suite → **2896 passed in 426.92s**, zero failures.

**Gate**

- WP-0A is complete. Its fresh-lock, smoke, static, migration/core and complete
  suite evidence is green, so WP-0B may now begin.

### WP-0B — Release gate and capability truth

**Status:** `completed`

**Outcome**

- CI 現在固定執行 exact hash install、manifest/lock sync、bounded TestClient
  smoke、Node primitive-only smoke、完整 collect、static invariant、
  migration/core、full suite 與 checkout pollution gate。
- CI 的 DB/audit/server config/auto-approve/home paths 全部指向
  `${{ runner.temp }}`，provider/credential settings 清空，external
  socket/DNS fail closed。
- `deploy/backup.sh` 改用 Python stdlib SQLite online backup，不再依賴系統
  `sqlite3` CLI；backup/restore round trip、拒絕未輸入明確 `yes`、displaced
  recovery 都只使用 temporary fixtures。
- 新增 capability ledger，嚴格區分 `implemented / test-only /
  default-enabled / deployed / canary-proven / production-ready`；沒有實地
  證據的欄位保持 `unknown/no`。
- Node 現況明確改為 protocol/client/runner primitives；因
  `agent/__main__.py` 不存在，service unit 只標為不可安裝的 template，
  沒有冒充 runnable daemon 或 canary-ready backend。
- 舊 roadmap/status 文件加入 `superseded_by` 與權威順序，不刪除歷史。
- 已用 characterization tests 固定三個 release blockers：
  stop kill unreachable 仍產生 false terminal、legacy dataset endpoint
  直接寫入且沒有 approval、server mutation 尚無 generic attempt/config
  revision guard。這些測試記錄缺陷，不把缺陷宣稱為正確 invariant。

**Changed**

- `.github/workflows/ci.yml`
- `.claude/skills/release-gate/scripts/static_checks.sh`
- `deploy/backup.sh`
- `scripts/sqlite_online_backup.py`
- `scripts/node_primitives_smoke.py`
- `docs/CAPABILITY_LEDGER.md`
- historical roadmap/status headers and Node service template
- WP-0B release/capability/authority/backup/Node/drift tests

**Evidence**

- Fresh locked Python 3.10 environment:
  `/tmp/dispatch-center-wp0a-clean-v2`。
- `pip install --require-hashes -r requirements.lock` → PASS。
- `python -m pip check` → `No broken requirements found.`。
- `python scripts/check_requirements_lock.py`
  → `PASS (11 direct requirements)`。
- backup/Node/capability/document/test-environment focused gate → 17 passed。
- stop/dataset/server characterization suites → 131 passed。
- migration/core suites → 88 passed。
- CI workflow contract → 3 passed。
- full discovery → 2909 tests collected。
- `DISPATCH_TEST_NETWORK=deny python -m pytest -q`
  → **2909 passed, 0 failed in 427.17s**。
- `bash .claude/skills/release-gate/scripts/static_checks.sh` → PASS。
- `git diff --check` and shell syntax checks → PASS。

**Safety / boundary**

- 未停止、重新載入或操作既有執行中服務；未連 production worker、OIDC、
  LLM 或外部服務，未讀 production credential。
- backup/restore test 只操作 pytest temporary directories；沒有對現存
  runtime DB 做備份、還原或 migration。
- GitHub-hosted CI 尚待 commit/push 後產生第一次 remote run；本紀錄只宣稱
  workflow contract 與等價本機 gate 通過。

**Gate**

- Phase 0 / WP-0A / WP-0B 本機實作與驗證完成。
- `DG-EXEC-ATTEMPT-v1` 已於 2026-07-27 具名核准；下一步為 WP-1A additive
  schema 與 pure domain tests。仍不切換 scheduler、不接觸 production
  SSH/Node backend。

### DG-EXEC-ATTEMPT — Decision-contract preparation

**Preparation status:** `completed`

**Decision status:** `approved 2026-07-27`

**Outcome**

- 新增 `docs/DG_EXEC_ATTEMPT_DECISION.md`，把 Phase 1 前置決策從 roadmap
  摘要展開為可逐條裁定的 exact contract。
- 固定 attempt/operation/Job projection、partial unique active constraint、
  immutable payload/target/authorization、outbox `effect_started_at` crash
  boundary、append-only events、shadow-only Phase 1 與 leader fencing。
- legacy approval/server/Node rows維持 honest legacy，不補造 digest、revision
  或 attempt；新 generic path 不設 nullable legacy authorization 例外。
- 明列 server delete/repoint/rotation dual-read guard、routine rotation 與
  emergency revoke 的不同語意。
- 確認本 gate 不修改 `jobs.status`、canonical invariants、SSH command/
  sentinel contract 或 production routing；ambiguous launch 仍留給
  `DG-AMBIGUOUS-LAUNCH`。
- 實作可行性複核補齊：
  - multi-Job approval 的 stable role/command digest mapping；
  - `servers.yaml` 與 SQLite 間的 prepared/publication mutation journal 及
    每一個 crash window；
  - reload 失敗時 only-exact-digest compensation，以及 digest drift 的
    `recovery_hold`；
  - 沒有 generic attempt 的 legacy running Job stop-intent；
  - Node protocol row linkage、non-cascading FK/immutable triggers。
- 第二次一致性複核再修正：
  - `uncertain` operation 只可依 read-only authoritative evidence 收斂，
    不會重送 material effect；
  - legacy SSH stop intent 與 generic `operation=stop` 分開持有，不雙寫成
    兩個真相來源；
  - credential revision 只用 `realpath/stat` 的版本化檔案參照，不開啟或
    hash 私鑰內容；
  - approval ID 與 payload digest 以不可變 tuple 綁定，避免把自增 ID
    嵌入自己的 hash 所造成的循環；
  - 所有 attempt 相關 runtime flags 預設關閉，已有 active ownership 時
    關閉 reconciler 會 fail closed。
- 同步修正 `docs/NEXT_IMPLEMENTATION_PLAN.md` 的 Phase 1 摘要，使其不再
  保留「generic attempt 可有 nullable legacy authorization」、私鑰內容 hash、
  approval ID 嵌入自身 digest 等已被 exact contract 收斂掉的舊描述。
- 新增 `tests/test_exec_attempt_decision_gate.py` 並由 static release gate
  釘住。核准前的 characterization 已證明新 schema、feature flags 與
  `attempt_cleanup` authority 沒有提前進入 runtime。

**Evidence**

- decision-gate characterization → 4 passed。
- decision/capability/document focused set → 10 passed。
- full discovery（含 decision-gate tests）→ 2913 tests collected。
- locked Python 3.10 complete suite
  → **2913 passed, 0 failed in 371.58s**。
- `DG-EXEC-ATTEMPT-v1` review document SHA-256
  → `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`。
- static invariant gate → PASS。
- `git diff --check` → PASS。

**Gate**

- 使用者於 2026-07-27 明確回覆
  `DG-EXEC-ATTEMPT：核准本文件的 recommended contract`。
- 此裁定已以 reviewed-draft SHA-256
  `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`
  綁定 `DG-EXEC-ATTEMPT-v1` 並寫入 `docs/DECISIONS.md`。
- WP-1A additive schema、窄版 DB/domain APIs 與 pure tests 已解除阻擋；
  production migration/cutover、scheduler 切換、遠端連線及後續 named gates
  均未獲授權。

### WP-1A — Attempt/outbox additive foundation

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-1A-0 decision authorization`：`completed`
   - 凍結核准當下 reviewed draft SHA-256。
   - 將具名裁定寫入 `docs/DECISIONS.md`，並把 decision document 改為
     approved。
   - 明列只解鎖 additive schema、窄版 API 與 pure tests；所有 execution
     flags 維持預設關閉。
2. `2026-07-27` — `WP-1A-1 additive schema and migration`：`completed`
   - 新增 `server_config_revisions`、`server_config_mutations`、
     `execution_attempts`、`execution_operations`、
     `execution_attempt_events`、`execution_shadow_observations`、
     `legacy_job_stop_intents` 與 `scheduler_leases`。
   - 對 `jobs`、`approvals`、`node_attempts` 採 additive nullable migration；
     representative legacy rows 維持 NULL，未補造 approval、digest、revision
     或 attempt。
   - 每個新 Database connection 在 schema initialization 前啟用並驗證
     `PRAGMA foreign_keys=ON`；新 FK 全部 `ON DELETE RESTRICT`，沒有 cascade。
   - 建立 active-attempt、active-revision、unresolved-mutation、legacy-stop
     partial unique indexes，以及 pinned approval/Job、target/attempt/operation
     identity、append-only event/shadow 的 DB triggers。
   - 新增 canonical JSON/command digest 驗證；float、NaN、非字串 key、
     command base64/digest 不一致均 fail closed。
   - 核准文件的 creation-event requirement 原先缺少成功 reason code；依該
     文件允許的「新增 code 必須有文件＋測試」規則，補入窄義
     `contract_validated`。它不表示 remote effect 已開始或完成。
   - WP-0B「新表不存在」characterization 已替換為 WP-1A schema-present、
     WP-1B dual-read guard 尚未接線的誠實契約。

   **Evidence**

   - fresh/legacy migration、immutability、race/outbox/config focused set
     → **111 passed**。
   - DB/API/approval/Node/server-config/background related set
     → **441 passed**。
   - static invariant gate → PASS。
   - `git diff --check`、Python compile → PASS。
   - 全部測試仍待 `WP-1A-3` 執行；此處不以 focused tests 取代 full gate。
3. `2026-07-27` — `WP-1A-2 transaction/CAS domain foundation`：`completed`
   - 新增 `BEGIN IMMEDIATE` material transaction boundary、SQLite-time
     scheduler lease acquire/renew/failover 與 live owner/epoch fencing。
   - attempt creation 會在同一 transaction 重驗 pinned Job/approval/command、
     active approved target revision、legacy Node owner、leader lease，建立
     attempt 與 append-only event；SSH 只建立 `dispatching` DB intent，
     沒有呼叫遠端。
   - Node generic path 只允許在同一 transaction 建立並連結新的
     `node_attempts.execution_attempt_id`；legacy Node rows維持 NULL，沒有
     retrofit。
   - attempt state/liveness 及 operation result 全採 expected-state CAS；
     unknown/unreachable 不改 Job terminal state，done/failed 只接受 matching
     terminal evidence。
   - outbox claim 只會重取 `effect_started_at IS NULL` 的過期 processing
     operation；effect boundary 之後過期會進 `uncertain`，不再送 material
     effect。
   - operation authorization class 重驗 actual approval kind/digest；
     execution approval 不可被 stop 重用。`inspect` 的 backend-specific fixed
     read-only allowlist 尚未由後續 gate/version pin 定義，因此 WP-1A API
     fail closed，不接受任意 shell payload。
   - server-config publication 以單一 transaction 建立 approval
     materialization marker、mutation intent 與 prepared revision；只有 exact
     before/after YAML digest 可 rollback/resume，第三 digest 進
     `recovery_hold`，final activation 同 transaction 更新 revision、journal
     與 approval。
   - 修正 leader failover fencing 語意：attempt 上的 epoch 是 immutable
     creation evidence；每次後續 mutation 另驗 current live lease，因此舊
     leader 被 fence、新 leader仍能 reconcile，而不改寫 creation epoch。
   - 補入 `remote_state_observed` reason code，僅代表 matching positive
     acknowledgement/receipt/sentinel/agent evidence；不由 reachability loss
     推導。
   - 四個 rollout flags 全部預設 `false`；`NEW_CLAIMS=true` 缺
     reconciler/outbox 時 config fail closed。有 durable active ownership
     而 `RECONCILE_EXISTING=false` 時，AppState 在任何 SSH/provider/background
     建立前拒絕啟動。

   **Evidence**

   - foundation/decision/config focused set → **83 passed**。
   - migration/server/Node/background related set + static gate
     → **396 passed**，static PASS。
   - two-connection active-attempt race、raw partial-unique bypass、
     stale/new leader failover、pre/post-effect claim expiry、server YAML
     drift/compensation 與 terminal evidence projection 均有專項測試。
   - 未建立 public API/UI/LLM/MCP mutation route，未接 scheduler/outbox
     background loop，未進行任何 SSH/Node/production DB 操作。
4. `2026-07-27` — `WP-1A-3 release evidence and handoff`：`completed`
   - full discovery → **2933 tests collected**。
   - 第一輪 full suite → **2932 passed, 1 failed**；唯一失敗是 WP-0B
     capability-ledger characterization 仍要求
     `execution_attempt_outbox.implemented=no`，與已完成的 WP-1A
     test-only foundation 不符。
   - 更新 ledger test 後，精確釘住 `implemented=yes`、`test-only=yes`，
     且 default/deployed/canary/production-ready 全為 `no`；其他未來
     cutover capability 仍為 `implemented=no`。
   - 第二輪 locked Python 3.10 full suite
     → **2933 passed, 0 failed in 492.89s**。
   - final static invariant gate、requirements lock sync、TestClient lifecycle
     smoke、`git diff --check` → 全部 PASS。
   - `tests/test_execution_attempt_foundation.py` 已加入 static
     `INV-TEST-2` pinning 清單。
   - 核准當下 reviewed-draft SHA-256 保持
     `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`；
     加入核准狀態與兩個依文件規則擴充且有測試的 reason/fencing clarification
     後，current implementation-contract document SHA-256 為
     `09aa99ebcc80e5b7884121aae3d4c290a0f9259cc8b605d66976c69f6a1e88dd`。

**WP-1A outcome**

- additive DB/domain foundation 完成，legacy SSH scheduler 行為未切換。
- 四個 flags 全為 default-off；沒有 production migration/deployment/canary
  證據，capability 正確標為 test-only、production-ready=no。
- 未操作現有 runtime DB、未連 SSH/Node/OIDC/LLM 或任何 production service，
  未讀 production credential。
- 下一工作包為 WP-1B；不可把 WP-1A schema/API pass 誤稱為 runnable
  attempt-driven execution。

**Next**

- `WP-1B`：pinned approval-to-Job publication、generic/legacy dual-read server
  mutation guard、shadow parity。所有 runtime flags 仍須維持關閉。

### WP-1B — Pinned publication, ownership guards and shadow parity

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-1B-1 atomic pinned Job publication`：`completed`
   - 新增 internal `materialize_pinned_execution_jobs()`；同一
     `BEGIN IMMEDIATE` transaction 重新驗證 pending approval、canonical
     payload/version/digest，寫 one-way materialization actor/mechanism，
     topological materialize 全部 role Job，最後才將 approval 設 approved。
   - `job_specs` 可依 stable role 指向 dependency role；publication 會解析
     dependency graph，產生實際 `depends_on` Job IDs，但 approved payload
     不嵌入 DB-generated ID。
   - 每個 Job 在 INSERT 同時固定 approval tuple、contract role、command
     digest 與 dispatch constraints；新增 partial unique role index，禁止同
     approval 重複 role。
   - 任何第二筆 Job 的 shape/constraint 錯誤會 rollback 第一筆 INSERT、
     materialization marker 與 actor attribution，不留下 partial chain。
   - 兩個 Database connections 同時 materialize 同一 approval 時恰好一個
     成功，另一個 fail closed；最後只有一條完整 Job graph。
   - 這是 internal DB/domain API；既有 public enqueue/approve、legacy SSH
     scheduler 與所有 runtime flags 均未變更。

   **Evidence**

   - foundation + representative legacy migration + decision gate
     → **58 passed**。
   - multi-Job reverse-order dependency、mid-publication rollback、
     two-connection race 與 unique role 均有專項測試。
2. `2026-07-27` — `WP-1B-2 dual-read server mutation guard`：`completed`
   - 新增 `get_server_execution_blockers()`，同時讀取 active generic
     attempt、pending/processing/uncertain operation、unlinked 且未過期/
     acknowledged/running 的 legacy Node attempt、enrolled non-revoked Node
     與 legacy running Job。
   - `server_delete` 在核准當下重驗上述 durable owners；任一存在即將
     approval 設 rejected，不改 `servers.yaml`、不清 in-memory config。
   - `server_update` 對 host/user/port/key/root sets/backend 的實際 repoint
     或 routine credential rotation 套用同一 guard；note/tags 等非 target
     欄位不被過度阻擋。Disable 仍保留設定與 revision 可解析性，沿用既有
     running-Job guard。
   - 任一 unresolved `server_config_mutations`
     `intent|yaml_applied|recovery_hold` 會全域阻擋既有 add/update/disable/
     delete approval 落地，legacy HTTP flow 不得繞過 recovery journal。
   - terminal workload 後仍 pending 的 collect operation 會繼續阻擋 target
     delete/repoint；已撤銷 Node 上 acknowledged legacy attempt 也不因撤權
     被誤當成可安全刪除。
   - 所有 rejection 只寫 DB decision/audit 與 blocker counts，不執行 SSH、
     不讀 credential bytes。

   **Evidence**

   - server-config API + foundation + attribution + approvals
     → **107 passed**。
   - enrolled Node delete/repoint、display-only update、unresolved global
     journal bypass、terminal-attempt pending operation、revoked-node legacy
     attempt 均有專項測試。
3. `2026-07-27` — `WP-1B-3 default-off shadow parity`：`completed`
   - 新增獨立 `execution_attempt_shadow_loop()`；只有
     `EXECUTION_ATTEMPT_SHADOW_ENABLED=true` 才執行，rollback/default-off
     時連 shadow DB method 都不呼叫。
   - 每輪只讀既有 Job、pinned approval、active approved target revision
     與 unresolved publication journal，且只 append
     `execution_shadow_observations`；不建立/更新 Job、attempt、operation、
     event、Node attempt 或 scheduler lease。
   - pinned contract/command digest/target revision 符合時記
     `eligible_*`；honest legacy Job、缺 approval/revision 或 digest 不符時
     記 `ineligible_*` 與 closed reason code，不補造歷史 approval、revision
     或 digest。
   - 同一 Job 狀態未改變時不重複產生觀測；狀態改變才記下一筆 before/after
     evidence。shadow 表的 UPDATE/DELETE 由 DB trigger fail closed。
   - loop 不接收 SSH/Node callback，也不呼叫 legacy scheduler；blocking DB
     call 仍納入 AppState shutdown drain，避免關閉 SQLite 的競態。

   **Evidence**

   - shadow/foundation/background/config/decision focused set
     → **101 passed**。
   - API/approval/server-config/Node/document expanded set
     → **179 passed**。
   - canonical tables 逐表 before/after snapshot、legacy ineligible evidence、
     missing target、append-only、zero attempt/outbox/lease、zero SSH 與
     default-off rollback 均有專項測試。
   - static invariant gate、requirements lock sync、Python compile、
     `git diff --check` → PASS。
4. `2026-07-27` — `WP-1B-4 release evidence and handoff`：`completed`
   - locked Python 3.10 full discovery → **2947 tests collected**。
   - external-network-denied full suite
     → **2947 passed, 0 failed in 449.41s**。
   - final static invariant gate、requirements lock sync、TestClient lifecycle
     smoke、Python compile、`git diff --check` → 全部 PASS。
   - 唯一 warning 是既知 Starlette TestClient/httpx deprecation；未影響測試
     結果，亦不是本工作包引入的 runtime failure。
   - 全程未連 SSH/Node/OIDC/LLM/production service，未讀 production
     credential，未修改現有 runtime DB；所有 execution rollout flags
     維持預設 `false`。

**WP-1B outcome**

- 完整 pinned multi-Job publication、server dual-read mutation guard 與
  default-off shadow parity 已完成並具 release evidence。
- shadow 只提供 append-only 測量；沒有 scheduler cutover、new claim、
  outbox worker 或 production attempt ownership。
- legacy SSH 仍是既有支援 backend；generic claim 仍須等待 pinned
  server-config public publication/recovery surface 及後續具名 cutover gate。

**Next**

- `WP-1C`：修正 legacy SSH stop false-terminal defect；approved stop 只保存
  durable intent，kill failure/unreachable 不得把 Job 標成 terminal，並防止
  stop 後被舊 scheduler 自動 requeue。這是 release blocker
  `RB-STOP-001`，不等同於啟用 generic execution。

### WP-1C — Legacy stop invariant correction

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-1C-1 pinned stop intent and delivery boundary`：
   `completed`
   - `request_stop_approval()` 改為在 INSERT 時建立 canonical
     `stop-intent-v1` payload digest；payload/version/timestamp 由既有 pinned
     approval trigger 保持 immutable。
   - 既有 pending unpinned stop approval 不補造 digest，核准時在任何遠端
     effect 前 fail closed，要求 reject/re-create。
   - 新增 legacy stop intent domain methods：在遠端前建立 `requested`，
     一次性跨越 delivery boundary 成為 `delivery_uncertain`，positive kill
     response 才成為 `delivered`。
   - crash/retry 若已跨 effect boundary 不重送 kill；已記 delivered 則只完成
     pending approval。raw remote exception 不寫入 stop-intent evidence，
     新表只存固定 category 與 sanitized detail。
   - generic attempt Job 不得建立 competing legacy intent；必須走後續
     generic stop operation。
2. `2026-07-27` — `WP-1C-2 canonical Job/reconcile correction`：
   `completed`
   - kill 成功或失敗都不再寫 `running → cancelled`/`finished_at`；成功表示
     delivered，失敗/timeout/unreachable 表示 delivery uncertain，Job 均
     保持 running。
   - unresolved stop intent 在同一 SQLite `BEGIN IMMEDIATE` 邊界阻止
     `running → queued`，並在 legacy scheduler 與 Node/SSH dispatch
     eligibility 兩層 fail closed，避免 tmux gone 後重新啟動。
   - matching done/failed reconcile 寫 Job terminal 時，同一 DB transaction
     把 intent 轉 `terminal_observed`；finish hook 仍只觸發一次。
   - unresolved intent 也納入 server delete/repoint ownership blocker；routine
     target mutation 不得讓 recovery 失去原 server identity。
   - 更新 REST/web-direct/auto-rule、Engineering Task/validation 與使用說明
     的舊 `cancelled` 契約；queued cancel 的既有 `cancelled` 行為不變。

   **Evidence**

   - approvals/API/scheduler/Node/server-config/Engineering/foundation focused
     set → **367 passed**。
   - pinned immutability、unreachable、definite pre-effect refusal、
     crash-no-replay、requeue/dispatch block、terminal convergence 與
     unpinned legacy refusal均有專項測試。
3. `2026-07-27` — `WP-1C-3 release evidence and handoff`：`completed`
   - locked Python 3.10 full discovery → **2951 tests collected**。
   - 第一輪 full gate 在舊 Agent tool assertion 發現仍期待
     `status=cancelled`；以 `-x` 精確重現為 1 failed / 182 passed，更新
     Agent/MCP fake response 與契約測試後，相關 set → **320 passed**。
   - 從第 1 項重跑 external-network-denied full suite
     → **2951 passed, 0 failed in 500.47s**。
   - final static invariant gate、requirements lock sync、TestClient lifecycle
     smoke、Python compile、`git diff --check` → 全部 PASS。
   - 唯一 warning 是既知 Starlette TestClient/httpx deprecation；未進行
     SSH/Node/production DB/credential 操作，所有 generic execution flags
     仍為預設 `false`。

**WP-1C outcome**

- `RB-STOP-001` 已解除：approved stop 不再製造 false terminal，unknown/
  unreachable 也不會 requeue 或重派；只有 matching terminal evidence
  收斂 done/failed。
- queued Job 的既有 user cancel 仍可寫 `cancelled`；本工作包沒有新增狀態或
  核准 `running → cancelled`，因此不需要也沒有改動 `DG-JOB-STATE`。
- legacy SSH backend 保留；本修正不是 generic attempt cutover，也未解決
  ambiguous launch。

**Next**

- `WP-2A`：minimum scheduler leader/fencing 與 outbox telemetry；只做
  internal/default-off ownership/observability。`WP-2B/2C` remote atomic
  launch/receipt/cutover 仍受 `DG-AMBIGUOUS-LAUNCH` 阻擋。

### WP-2A — Minimum scheduler ownership and execution telemetry

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-2A-1 process ownership loop`：`completed`
   - `AppState` 新增 process-opaque owner UUID 與獨立 ownership loop；只有
     `NEW_CLAIMS`、`RECONCILE_EXISTING` 或 `OUTBOX_WORKER` 任一明確設定時，
     才讀寫既有 `scheduler_leases`。
   - lease acquire/renew 沿用 WP-1A 的 SQLite-time `BEGIN IMMEDIATE`
     transaction 與 fencing epoch；同 DB 的第二個 process 只能保持
     non-leader，無權建立 attempt、claim operation 或寫 transition。
   - lease duration 是 scheduler interval 的三倍（最低 5 秒）；shutdown
     不刪 durable lease，由 expiry/fencing 接手，避免把 process exit 誤當
     ownership transfer evidence。
   - default configuration 完全不建立 lease；loop 不接收 SSH/Node callback，
     未新增 remote launch/reconcile/outbox effect。
2. `2026-07-27` — `WP-2A-2 evidence-backed telemetry`：`completed`
   - 新增 read-only `Database.get_execution_control_plane_telemetry()`；lease
     liveness、queue/unknown/uncertain age 全以 SQLite clock 計算，不混用
     process wall clock。
   - telemetry 包含 attempt by state/backend、active unknown age、outbox
     backlog/uncertain age、operation/collection state、Job queue depth/age，
     以及只計既有 `claim_conflict` event 的 recorded duplicate-prevention
     lower bound。
   - `GET /execution-control/status` 已登錄為 authenticated platform-view
     read surface；回傳 current-process leader 判定與 rollout flags，但不回
     credential、remote log 或任意 command。
   - remote reconciler/outbox/new-claim worker 尚未實作時，status 明確回
     `implemented=false`、`reconciler_last_success_at=null`；不把 lease
     renewal 冒充 remote reconciliation success。
   - 本包重用既有 tables，沒有 schema migration，也沒有 fabrication/
     backfill historical attempt、revision、digest 或 metrics。
3. `2026-07-27` — `WP-2A-3 release evidence and handoff`：`completed`
   - foundation/background/API/config/authorization focused regression
     → **178 passed**。
   - 專項測試涵蓋 default-off 零 lease write、雙 process 單一 leader、
     loser fail closed/零 remote call、durable telemetry aggregate、空資料
     `null` 語意與 read route 零 mutation。
   - external-network-denied full suite
     → **2955 passed, 0 failed in 502.05s**。
   - document/capability/decision/CI/UI/authorization handoff set
     → **56 passed**。
   - static invariant gate、requirements lock sync、Python compile 與
     `git diff --check` → PASS。
   - 唯一 warning 仍是既知 Starlette TestClient/httpx deprecation；未連
     SSH/Node/OIDC/LLM/production DB，所有 execution flags 維持預設
     `false`。

**WP-2A outcome**

- minimum single-leader/fencing ownership 與 canary 前所需的 durable
  execution telemetry 已具 runtime surface 及 local regression evidence。
- 這不等於 generic scheduler/outbox 已可執行：status 會誠實顯示三個 remote
  worker 尚未實作，`execution_attempt_outbox` 仍非 deployed、非
  canary-proven、非 production-ready。
- `RB-LAUNCH-001` 未解除；沒有 atomic remote claim、receipt/token validation
  或 response-loss recovery，legacy SSH 仍是唯一支援執行路徑。

**Next**

- `WP-2B/2C` 仍受 `DG-AMBIGUOUS-LAUNCH` 阻擋。下一個可安全進行的工作是先
  提交該 gate 的 exact `INV-STATE-2` 修訂、definite-prelaunch boundary、
  atomic claim/receipt 與 replay contract 供核准；核准前不得實作或啟用
  remote attempt path。

### DG-AMBIGUOUS-LAUNCH / DG-EXEC-ATTEMPT-v1.1 — Gate rulings

**Status:** `approved (documentation only; no code changed)`

**Task log**

1. `2026-07-27` — `Defect re-verification before drafting`：`completed`
   - `app/scheduler.py:538-566` 確認為無條件 revert：`dispatch_job()` 的任何
     例外都執行 `db.update_job(job.id, status="queued", server=None,
     started_at=None)`。
   - `dispatch_job()`（`app/scheduler.py:297-308`）先 `prepare()` 後
     `launch()`，而 `launch()` 送出的是
     `tmux new-session -d -s job_{id} 'bash …/run.sh'`
     （`app/jobqueue.py:542-544`），故 tmux 已 fork 但 response lost 時仍會
     revert。`app/scheduler.py:529-537` 的既有註解只涵蓋 crash window。
   - `app/jobqueue.py:574-608` 的 double-check 無法補償此路徑，因為 revert
     之後 Job 已無 `server` 可供 reconcile。
2. `2026-07-27` — `DG-AMBIGUOUS-LAUNCH-v1 draft`：`completed`
   - 新增 `docs/DG_AMBIGUOUS_LAUNCH_DECISION.md`（493 行），內容涵蓋
     `INV-STATE-2` 逐字修訂、definite/ambiguous 分類、atomic remote claim /
     receipt / trap sentinel 協議、unknown 解析程序、additive schema delta、
     flags/rollout、crash matrix 與五項子裁定。
   - 核心設計為 **arbitration, not observation**：控制端必須自己贏得同一個
     `mkdir` claim 才能宣告 `abandoned_before_launch`，藉此消除
     settle-window 競態。
3. `2026-07-27` — `DG-EXEC-ATTEMPT-v1.1 addendum draft`：`completed`
   - 於 `docs/DG_EXEC_ATTEMPT_DECISION.md` 末尾附加 `Addendum v1.1`，
     §1–12 的已核准 v1 文字逐字未動，並在 addendum 檔頭記錄附加前 digest
     `09aa99eb…` 以保留證據鏈。
4. `2026-07-27` — `使用者裁定與記錄`：`completed`
   - 兩份草稿經使用者審閱後核准，具名裁定與 reviewed-draft SHA-256
     （`d38f2dea…`、`3a8eb432…`）記錄於 `docs/DECISIONS.md`。
   - `DG-AMBIGUOUS-LAUNCH-v1` 的 D-1…D-5 五項子裁定全部採用建議值。
   - 同步更新 `docs/NEXT_IMPLEMENTATION_PLAN.md` 檔頭狀態、§12 gate 表、
     §14 WP-2B/2C 阻擋狀態與 §16 下一切片，以及
     `docs/CAPABILITY_LEDGER.md` 的 `RB-LAUNCH-001`／`RB-SERVER-001` 兩列。

**Outcome**

- 兩個 gate 已核准，WP-2B/2C 與 `RB-SERVER-001` 的實作獲得授權。
- **沒有任何程式碼、schema、flag 或 canonical invariant 被修改。**
  `RB-LAUNCH-001` 仍然開啟：`app/scheduler.py:557` 的缺陷未動，legacy SSH
  仍是唯一支援的執行路徑。gate 核准不是修復。
- `INV-STATE-2` 的條文**刻意尚未寫入**
  `.claude/skills/dispatcher-domain/references/invariants.md`：其
  Verification 欄引用的 `tests/test_execution_launch_arbitration.py` 尚不
  存在，條文與測試必須在 WP-2B 同一個工作包內一起落地。

**Next**

- `WP-2B`：依 gate §3.2/§4/§5/§7/§12 實作 additive schema、pure builder、
  versioned launcher golden 與 FakeSSH crash matrix，並在同一包內 enact
  `INV-STATE-2` 新條文。不接 scheduler dispatch 路徑，flags 全部維持關閉。

### WP-2B — SSH launch arbitration primitives

**Status:** `completed`

**Task log**

1. `2026-07-27` — `WP-2B-1 canonical INV-STATE-2 amendment`：`completed`
   - 依 `DG-AMBIGUOUS-LAUNCH-v1` §3.2 逐字改寫
     `.claude/skills/dispatcher-domain/references/invariants.md` 的
     `INV-STATE-2`：無條件 revert 條款改為
     **只有 definite pre-launch failure 可 `running → queued`**。
   - 新增 **Legacy exception** 欄位，明文記載 `app/scheduler.py` 的 legacy
     派發路徑仍無條件 revert，屬已登記缺陷 `RB-LAUNCH-001`，由 WP-2C 收斂；
     不得被引用為新程式碼的先例。條文與其 Verification 引用的測試檔在同一個
     工作包內落地。
2. `2026-07-27` — `WP-2B-2 pure launch primitives`：`completed`
   - 新增 `app/execution_launch.py`：attempt-scoped 路徑、per-attempt tmux
     session 命名、prepare/launch/abandon/inspect 純 builder、versioned
     launcher/wrapper bytes（`LAUNCHER_CONTRACT_VERSION = "v2"`）。
   - `mkdir "$D/claim"`（無 `-p`）是唯一仲裁原語；prepare 用 `mkdir -p`
     故無法仲裁；receipt 以 temp file + atomic rename 發布；wrapper 以 bash
     trap 產生真正的數字 sentinel，controller 不偽造終態。
   - 只有經驗證的整數 job id 與 UUID attempt id 會進入指令字串；fencing
     token 與使用者 command bytes 僅以 SFTP 檔案內容落地（`INV-SSH-4`）。
   - inspect 在 tmux 探測前後各讀一次 sentinel，關掉「工作剛好在探測間隙
     完成」被誤讀為消失的競態。
3. `2026-07-27` — `WP-2B-3 classification and resolution`：`completed`
   - definite/ambiguous 分類為封閉 allowlist，**預設 ambiguous**；未列舉的
     例外型別一律 ambiguous。transport 證據在 `effect_started` 之後不可採信
     （gate §4.1 rule 2），此後只有仲裁能產生 definite verdict。
   - `resolve_attempt_observation()` 實作 gate §6 解析順序；唯二可 requeue
     的情形是 controller 贏得 claim，以及 boot_id 變更（重開機為工作已死的
     正面證據）。「沒看到 tmux/sentinel」永遠不構成 requeue。
4. `2026-07-27` — `WP-2B-4 additive schema delta`：`completed`
   - `execution_attempts` 增 `remote_claim_state`、`launch_receipt_sha256`、
     `remote_boot_id`、`launcher_contract_version`、`prelaunch_verdict`；
     `execution_operations` 增 `transmission_state`；
     `server_config_revisions` 增 `attempt_backend_preflight`。
   - 新 DB 由 CREATE TABLE 的 CHECK 約束值域；`ALTER TABLE ADD COLUMN`
     無法帶 CHECK，因此另加 trigger 讓既有 DB 得到相同強度，並加上
     settled claim 單向、receipt/boot_id 證據不可改寫、
     `not_transmitted` 在 `effect_started_at` 之後不可宣稱等守衛。
   - legacy row 全部維持 `NULL`，不回填、不捏造觀測。
5. `2026-07-27` — `WP-2B-5 release evidence`：`completed`
   - 新增 `tests/test_execution_launch_arbitration.py`（54 tests），涵蓋
     gate §12 crash matrix：claim/tmux/receipt 三個崩潰點、雙 launcher 競爭、
     controller 仲裁對上進行中 launcher、response lost（`RB-LAUNCH-001`
     情境）、token 不符、跨 attempt 證據、重開機、探測間隙完成、unreachable。
   - 另以真實 `bash` 在隔離 scratchpad 執行 launcher bytes 驗證：第二個
     launcher 回 `LAUNCH_CLAIM_TAKEN` 且零 session；controller 先贏時
     launcher 全程未建立 tmux；trap 寫出 exit code 3；receipt key 為
     canonical 排序。未接觸任何 production worker 或 runtime 檔案。
   - 相關子系統 → **207 passed**；static invariant gate → **PASS**
     （新測試檔已納入 INV-TEST-2 pinning 清單）；
     external-network-denied full suite → **3009 passed, 0 failed in 569.91s**。

**WP-2B outcome**

- `INV-STATE-2` 的新語意已成為 canonical 條文，並有可執行的 crash matrix
  作為 Verification。
- 仲裁原語、分類與解析皆為純函式，可直接被 WP-2C 的 dispatch 路徑取用。
- **`RB-LAUNCH-001` 仍未解除**：`app/scheduler.py` 尚未改接 attempt-driven
  路徑，legacy 無條件 revert 一行未動。本包刻意不接 dispatch，並以
  `test_scheduler_does_not_import_the_new_launch_module_yet` 靜態鎖住此邊界。
- 所有 execution flags 維持預設 `false`；未新增 public/LLM/MCP route。

**Next**

- `WP-2C`：把 dispatch/reconcile/stop/collect 改由 attempt 驅動，接上本包的
  仲裁原語，解除 `RB-LAUNCH-001`；仍不啟用 production flag，canary 屬 WP-2D。

### WP-2C — Attempt-driven SSH dispatch

**Status:** `completed（code-complete；canary 未做，RB-LAUNCH-001 尚未關閉）`

**Task log**

1. `2026-07-27` — `WP-2C-1 attempt-driven lifecycle`：`completed`
   - 新增 `app/execution_dispatch.py`：`dispatch_job_via_attempt()` 依
     gate §7.2 順序執行——先在單一 DB transaction 建立 attempt、固定
     server/backend/revision 並把 Job `queued → running`，commit 前不做任何
     mkdir/SFTP/SSH；prepare 與 launch 皆為 durable outbox operation，各自在
     material call 之前寫 `effect_started_at`。
   - launch 的 `idempotency_key` 為 `launch:{attempt_id}:{fencing_token}`，
     replay 重用同一列，不建立第二個 operation。
   - `reconcile_attempt()` 只讀，狀態變更一律由 WP-2B 的
     `resolve_attempt_observation()` 推導；`arbitrate_unknown_attempt()` 讓
     控制端去搶同一個 claim，贏了才可 requeue。
2. `2026-07-27` — `WP-2C-2 canonical Job convergence`：`completed`
   - `Database.requeue_job_after_abandoned_attempt()` 只接受
     `abandoned_before_launch` 且 `liveness=known` 的 attempt；
     `apply_attempt_resolution_to_job()` 的 done/failed 必須與 attempt 的
     terminal state 相符，queued 投影必須來自 failed（重開機證據）。
     投影無法憑空產生 attempt 本身沒有的終態。
3. `2026-07-27` — `WP-2C-3 guard reconciliation`：`completed`
   - **WP-1A 的 abandon 守衛過寬**：原本只要「任何 operation 已開始 effect」
     就拒絕 abandon。收窄為只看 `launch` operation，且允許
     `transmission_state='not_transmitted'`——prepare 開始後失敗
     （SSH 連線被拒、launcher 從未被呼叫）正是 INV-STATE-2 允許 requeue 的
     definite pre-launch failure。
   - **WP-2B 的 trigger 與仲裁機制矛盾**：原 trigger 禁止「effect 已開始還
     宣稱 not_transmitted」，但控制端仲裁的全部意義就是在 effect 開始之後
     才建立「未傳輸」的證明。移除該條件，改由
     `transition_execution_attempt` 的 abandon 守衛把關，並在 schema 註記
     為何這條規則不能是靜態 trigger。
   - `liveness unknown` 的合法 reason code 增列 `effect_outcome_unknown`
     （launch response 遺失時主機可能完全可達，未知的是 effect 是否生效）；
     `unknown → known` 增列 `pre_effect_definite_failure`，否則會與
     abandon 守衛要求的 reason code 互斥。
   - `insert_execution_operation` 的 `inspect` 由 WP-1A 的一律拒絕，改為
     只接受 WP-2B 已釘住的 `command_kind` allowlist；任意 payload 仍然
     fail closed。
4. `2026-07-27` — `WP-2C-4 rollout gating`：`completed`
   - 新增 `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED`（預設 `false`），啟用時
     必須同時 `EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true`，否則在 background
     loop 啟動前於設定驗證階段就失敗。
   - `AttemptLaunchContext.owns()` 保守判定：flag 開啟、本 process 目前持有
     leader lease、且該台機器有 approved pinned revision，三者皆成立才由
     attempt path 接管；任何不確定一律留在 legacy SSH 路徑。
   - scheduler 的 legacy 無條件 revert 分支保留且逐字不變，只在 attempt path
     未接管該機器時執行；就地補上註解說明它是 `RB-LAUNCH-001`，不得作為
     新程式碼先例。
5. `2026-07-27` — `WP-2C-5 release evidence`：`completed`
   - 新增 `tests/test_execution_attempt_dispatch.py`（18 tests）。關鍵案例
     `test_response_lost_after_tmux_keeps_the_job_running` 直接重現
     `RB-LAUNCH-001`：launch 逾時後 Job 維持 `running`、attempt
     `liveness=unknown`，不再退回 queue。
   - 另涵蓋：遠端呼叫前 DB intent 已 commit、ambiguous 不建立第二 attempt、
     definite transport failure 才 requeue、DB 層拒絕從 ambiguous requeue、
     仲裁贏/輸/不可達、terminal sentinel 收斂、缺證據不 requeue、
     flag/leader/revision 三重 gating、inspect allowlist。
   - 修正測試污染：原用 `asyncio.get_event_loop()`，在其他測試關閉 loop 後
     會 `RuntimeError`（單獨跑全過、全套跑掛 14 個），改用 `asyncio.run()`。
   - 相關子系統 → **298 passed**；static invariant gate → **PASS**；
     external-network-denied full suite → **3027 passed, 0 failed in 580.88s**。

**WP-2C outcome**

- `RB-LAUNCH-001` 的缺陷行為在 attempt path 已修正並有直接對應的測試。
- **但 blocker 尚未關閉**：flag 預設關閉，production 仍走 legacy 無條件
  revert 分支；沒有任何部署或 canary 證據。ledger 因此記為
  「fixed in code, not yet proven in operation」，`deployed`/`canary-proven`
  維持 `no`。
- 沒有新增 public/LLM/MCP mutation route；`jobs.status` 狀態集合未變；
  legacy SSH builder 與 golden fixtures 逐字不變。

**Next**

- `WP-2D`：當時的 v1 合約要求非 production worker 上 ≥20 jobs / 24
  小時 canary；2026-08-01 的 `DG-WP2D-CANARY-v2` 已將現行窗改為 8
  小時。其他條件仍含一次強制
  response-loss 與一次 control-plane restart，以及 rollback drill。通過後才
  能關閉 `RB-LAUNCH-001` 並考慮啟用 flag。

### Phase 2 completion — backend convergence and WP-2D readiness

**Status:** `code-complete；WP-2D canary 未執行，RB-LAUNCH-001 仍未關閉`

**Task log**

1. `2026-07-27` — `stop/collect 收進 attempt outbox（gate §7.3）`：`completed`
   - 新增 `build_attempt_stop_command()`：只殺**該 attempt** 的 session。
     legacy 的 `tmux kill-session -t job_{id}` 會連同一個 Job 的其他 attempt
     一起殺掉，per-attempt session 命名是這裡安全性的來源。
   - `stop_attempt()` 綁 `kind=stop` approval（DB 以 `authorization_class`
     強制，原 execution approval 無法冒充），且**送達不是終態證據**：成功只
     代表訊號送出，attempt 仍等 wrapper trap 寫出的數字 sentinel 才收斂；
     送達失敗則完全不動 attempt。
   - `collect_attempt()` 為獨立 outbox operation，失敗只記在該 operation，
     不回寫 workload 狀態——exit 0 的 Job 即使拉不到 artifact 仍是 `done`。
2. `2026-07-27` — `發現並修正 stop 契約互斥`：`completed`
   - WP-1A 的 attempt stop 授權要求 approval payload 帶 `attempt_id`，但
     WP-1C 把 stop 契約釘成**恰好** `{job_id, source}` 兩個鍵，兩個驗證器
     互斥，attempt-scoped stop 自始無法通過任何一條路徑。
   - 改為接受兩種形狀：legacy 兩鍵（走 legacy SSH），以及三鍵帶
     `attempt_id`（attempt 路徑）。若不釘 attempt，某個 attempt 的 stop
     核准就能拿去殺同一 Job 的後續 attempt。
3. `2026-07-27` — `versioned exact goldens（gate §8）`：`completed`
   - `_GOLDEN_V2` 逐字釘住 prepare/launch/abandon/stop/collect 五條指令與
     launcher/wrapper 檔頭；測試同時斷言 `LAUNCHER_CONTRACT_VERSION == "v2"`，
     改版必須新增 fixture 並保留舊版，讓升級後仍能解讀在途 attempt。
4. `2026-07-27` — `WP-2D 工具與手冊`：`completed`
   - 新增 `scripts/canary_report.py`：以 immutable 模式唯讀開啟 DB，從持久化
     證據計算 gate §9 的八條 pass/fail，exit 0/1/2。判定來自資料，不是操作者
     對那個觀察窗的印象。
   - 新增 `tests/test_canary_report.py`（6 tests）：乾淨窗通過、job 數不足、
     unresolved unknown、重複 launcher claim 皆正確 FAIL；DB 不可讀回 exit 2；
     並斷言腳本執行前後 DB bytes 完全相同。
   - 新增 `docs/WP_2D_CANARY_RUNBOOK.md`：前置條件（含 `agent_jobs` 必須在
     本機檔案系統的 `stat -f` 驗證）、四個 flag 的啟用順序、三項必做演練
     （強制 response-loss、control-plane restart、rollback drill）與各自的
     失敗信號、判定與後續處置。
   - full suite → **3040 passed, 0 failed in 574.80s**；static gate → PASS。

**Outcome**

- Phase 2 的**程式碼**部分完成：六個 operation 全部經 attempt outbox，
  stop/collect 語意正確，golden 已版本化。
- **Phase 2 本身尚未完成**：WP-2D 需要真實非 production worker 與現行
  v2 8 小時
  觀察窗，無法在開發階段執行。`RB-LAUNCH-001` 維持開啟，
  `attempt_driven_ssh` 的 `deployed`/`canary-proven` 維持 `no`。

**Next**

- 由操作者依 `docs/WP_2D_CANARY_RUNBOOK.md` 執行 canary，再以
  `scripts/canary_report.py` 產生判定。通過才可關閉 `RB-LAUNCH-001`。

### RB-SERVER-001 — Pinned server-config publication (partial)

**Status:** `server_add 完成；update/disable/delete 與 operator recovery surface 未做`

**Task log**

1. `2026-07-27` — `publication 模組`：`completed`
   - 新增 `app/server_publication.py`，依 `DG-EXEC-ATTEMPT-v1.1` addendum
     把既有 server approval 的**執行面**改走已核准的 publication protocol
     （`intent → yaml_applied → activated`），不新增 approval kind、不新增
     public route。
   - `normalize_target()` 只取 backend/host/port/user/roots 六個欄位；note、
     tags、idle 門檻等 operator metadata 刻意不納入 target identity，改備註
     不會讓已 pin 的 attempt 目標失效。
   - `credential_reference()` 只記路徑與 `file_identity`（device/inode/size/
     mtime_ns），永不記金鑰內容；inode 變更即代表換了憑證，pin 舊憑證的
     attempt 不會默默跟著換。
2. `2026-07-27` — `approve() 接線`：`completed`
   - `server_add` 的 YAML 寫入被包進 protocol，durable intent 先於檔案變更。
   - 未 pin 的 legacy approval 不發布：仍照舊寫 YAML 並誠實停在
     `legacy_observed`，不替沒被該契約審過的列回填證據。
3. `2026-07-27` — `補償依磁碟實況，不依假設`：`completed`
   - 初版在寫入失敗時一律標 `recovery_hold`，被 DB 守衛擋下
     （`recovery_hold requires a third or unreadable digest`）——**守衛是對的**：
     乾淨失敗時檔案仍是 before digest，正確轉換是 `rolled_back`。
   - 改為以 `observe_yaml()` 重讀檔案再決定：before → `rolled_back`
     （prepared revision 補償成 `retired`）；after → 寫入其實成功，繼續
     `activated`；第三種或讀不到 → `recovery_hold` fail closed。
     假設「失敗就是乾淨失敗」正是半寫入設定變成無人察覺的 active target
     的成因。
4. `2026-07-27` — `測試與驗證`：`completed`
   - 新增 `tests/test_server_publication.py`（9 tests）：eligibility 由
     `legacy_observed` 變 `approved`/`active`、intent 先於寫入、三種補償
     分支、legacy 未 pin 不發布、target identity 忽略 metadata、憑證參照
     不含金鑰內容、YAML digest 與落盤內容一致。
   - 相關子系統 → **108 passed**；static gate → PASS；
     full suite → **3049 passed, 0 failed in 578.79s**。

**Outcome**

- 核准新增的 server 現在會產生 pinned immutable revision，`create_execution_attempt()`
  不再因 `legacy_observed` 拒絕該目標——這是 `NEW_CLAIMS` 能啟用的前提。
- **未完成**：`server_update`/`server_disable`/`server_delete` 仍直接寫 YAML；
  `recovery_hold` 的 operator recovery surface 尚未實作。
  `NEW_CLAIMS` 維持 `false`。

### RB-SERVER-001 — Completed

**Status:** `completed（blocker 已關閉）`

**Task log**

1. `2026-07-27` — `update / disable / delete 接線`：`completed`
   - `server_update` 發布**新的** pinned revision，舊 revision 轉 `retired`
     而非就地修改——pin 舊 revision 的 attempt 仍能解析原目標供 reconcile。
   - `disable`/`delete` 不產生新目標，只把 active revision 退役；journal
     仍記錄該次 mutation。
   - 非 `add` 操作一律帶 `prior_revision_id` 並與當前 active revision 比對，
     與其他發布競態時 fail closed，不會退役別人已替換掉的 revision。
2. `2026-07-27` — `修正自己引入的靜默失效`：`completed`
   - 初版在「legacy 機器沒有 active revision」時直接 return，**沒有呼叫
     `write_yaml()`**——那會讓 legacy 機器的停用/刪除完全不生效，操作者按了
     沒反應。已補上寫入並加 regression test
     `test_disabling_a_legacy_server_still_writes_yaml`。
3. `2026-07-27` — `request/approve 之間的 digest 漂移`：`completed`
   - approval 釘住審閱當下的 before/after digest。若 servers.yaml 在等待期間
     被改動，發布會啟用一個沒人審過的目標，因此改為丟
     `ServerPublicationRejected` 且**完全不寫檔、不留 journal**，由操作者
     依當前狀態重新提出。
4. `2026-07-27` — `operator recovery surface`：`completed`
   - `GET /server-config/journal`（PLATFORM_VIEW）只回狀態與 digest，
     不回 YAML 內容、憑證或金鑰路徑。
   - `POST /server-config/journal/{id}/resolve`（PLATFORM_MANAGE）讓操作者
     宣告磁碟上實際觀察到的 digest。該 digest 必須等於 journal 自己記錄的
     before 或 after，否則拒絕、hold 維持——操作者只能在兩個已記錄的結果中
     擇一，不能捏造 revision、改 digest 或讓兩個 revision 同時 active。
   - 兩條路由都登錄進 authorization catalog，且不是 LLM/MCP 工具。
     `tests/test_authorization_coverage.py` 的路由計數由 117 更新為 119
     ——該計數是刻意的閘門，新路由必須被有意識地分類與計數。
5. `2026-07-27` — `驗證`：`completed`
   - `tests/test_server_publication.py` 共 **16 tests**；
     相關子系統 → 88 passed；static gate → PASS；
     full suite → **3056 passed, 0 failed in 580.26s**。

**Outcome**

- `RB-SERVER-001` **已關閉**。四種 server mutation 全部經 publication
  protocol，通過此路徑發布的目標具備 generic claim 資格。
- 未 pin 的 legacy approval 仍照舊運作並誠實停在 `legacy_observed`，
  不回填證據。
- `NEW_CLAIMS` 仍需 WP-2D canary 才可啟用——本工作包解除的是資格問題，
  不是實機驗證。

### WP-3A — Immutable dataset snapshots (pipeline)

**Status:** `completed（pipeline 與 schema；尚未接進任何 run）`

**Task log**

1. `2026-07-27` — `DG-DATASET-SNAPSHOT-v1 核准並記錄`：`completed`
   - reviewed-draft `da53f51d…` 對應 commit `b5f2627`，**digest 可驗證**
     ——這正是 `DG-EXEC-ATTEMPT-v1` 缺少的環節。
   - D-1…D-5 全部採用建議值。
2. `2026-07-27` — `deterministic 內容 manifest`：`completed`
   - `build_candidate_manifest()` 對每個 byte 算 SHA-256（D-2），並在讀完後
     **重新 `lstat`**：size 或 mtime_ns 變動代表來源在讀取途中被改動，剛算出
     的 digest 描述的是已不存在的 bytes → `verification_unknown`，不產生
     假 hash。
   - symlink 記為連結、永不跟隨（跟隨會把來源樹以外的 bytes 悄悄拉進來）；
     device/FIFO/socket 直接拒絕建置，它們不是資料也無法從 manifest 重建。
   - 超過 `DATASET_SNAPSHOT_MAX_BYTES`（預設 200 GiB）拒絕建置，不抽樣。
3. `2026-07-27` — `deterministic shard`：`completed`
   - tar 正規化 mtime/uid/gid/uname/gname/mode，USTAR 且不壓縮（gzip 會嵌入
     時間戳）。測試以兩棵 mtime 不同的相同內容樹驗證 shard digest 相同。
   - shard 邊界是 manifest 與 policy 的純函式；超過 `max_shard_bytes` 的
     單檔自成一個 shard，不切分。
4. `2026-07-27` — `LocalArtifactStore 與 publish`：`completed`
   - staged shard **從磁碟重讀**再驗 digest 與 size；只驗記憶體裡的值證明
     不了實際落盤的東西。
   - blob 以 content-addressed 路徑落地，相同 digest 視為 dedup 而非錯誤；
     descriptor 以 temp + atomic rename 發布，半寫入狀態永不可見。
   - `preflight()` 偵測非本機檔案系統（NFS/CIFS/FUSE）→
     `ineligible_non_local_fs`，發布依賴同檔案系統內的 `rename(2)` 原子性。
   - `build_and_publish()` 重算 candidate digest 並與核准值比對，
     不符即 `source_drifted_since_request` 中止——這一步擋掉「request 與
     approve 之間被改動的來源被當成已審閱內容發布」。
5. `2026-07-27` — `schema 與 legacy 邊界`：`completed`
   - `dataset_snapshots` / `dataset_snapshot_shards` 為 additive；
     `published` 列由 trigger 鎖成不可改寫、不可刪除；CHECK 確保
     `published` 必須帶 manifest_digest、descriptor_path 與 build approval。
   - `datasets.reproducible` 加入 SCHEMA 與 `_DATASET_COLUMN_MIGRATIONS`
     雙軌（`INV-STATE-3`），**任何 migration 都不會把它設成 1**；
     legacy DB 遷移測試直接斷言這點。
6. `2026-07-27` — `驗證`：`completed`
   - `tests/test_dataset_snapshot.py` **20 tests**；
     相關子系統 → 119 passed；static gate → PASS；
     full suite → **3076 passed, 0 failed in 601.34s**。

**Outcome**

- 系統現在**可以陳述一次 run 消耗了哪些 bytes**——這是 Phase 3 可重現性的
  前提，也是 legacy `{path, size}` manifest 永遠做不到的事。
- **尚未接進任何 run**：JobSpec/ExecutionPlan 綁定 snapshot、以及
  `dataset_not_reproducible` 的 request-time 拒絕，都屬 WP-3B。
  兩個 flag 維持預設關閉，沒有發布過任何真實資料集。
- `RB-DATASET-001` 仍開著：標記已就位，但 D-5 要求的拒絕行為要等 WP-3B。

**Next**

- `WP-3B`：ExecutionPlan/Run schema、preview/request/approval binding、
  planner-driven SSH 執行，並在 run request 時對 legacy dataset 回
  `dataset_not_reproducible`。

### WP-3B (partial) — ExecutionPlan derivation

**Status:** `planner 核心與 schema 完成；API 端點與 approve-time 接線未做`

**Task log**

1. `2026-07-28` — `immutable plan binding`：`completed`
   - 新增 `app/execution_plan.py`。ExecutionPlan 只綁**不可變 revision
     識別碼**：`project_versions.id`（釘住的 commit）、`published` 的
     `dataset_snapshots.id`、`run_profiles.id`（特定 revision 而非
     `(project, name)` 頭部）、`assignment_eligibility='approved'` 的
     `server_config_revisions.id`，以及指令自身的 SHA-256。
   - 綁可變 head 會讓一次 run 在 preview → 核准 → 執行之間改變意義而沒有
     任何紀錄，那正是這套設計要防的事。
2. `2026-07-28` — `純函式 preview`：`completed`
   - `derive_plan_draft()` 無寫入、無遠端呼叫，因此 preview 端點可以是真正
     的「讀」——使用者能問「這樣跑會怎樣」而不建立任何東西。
   - 所有阻擋原因一次回報，不在第一個短路；reason code 為封閉集合，越界
     直接丟 `ValueError`。
3. `2026-07-28` — `D-5：legacy dataset 在 request 時拒絕`：`completed`
   - legacy registry dataset 得到 `dataset_not_reproducible`，`plan_digest`
     為 `None`，approve-time 驗證永遠無法通過——**拒絕而非靜默降級**。
   - `dataset_none` 與「沒選資料集」是**不同的 plan**，digest 也不同：
     「明確不用資料」是可重現的陳述，「某個目錄」不是。
4. `2026-07-28` — `schema`：`completed`
   - `execution_plans` 為 additive，CHECK 強制 `reproducible=1` 必須同時
     釘住 code、profile 與（snapshot 或 dataset_none）；trigger 讓已建立的
     plan 不可改寫、不可刪除——plan 是以 digest 被核准的，digest 涵蓋的
     欄位事後都不能動。
5. `2026-07-28` — `驗證`：`completed`
   - `tests/test_execution_plan.py` **18 tests**，含「digest 對每一個綁定
     revision 的變動都敏感」的逐欄位掃描——那是 approve-time 重驗有意義的
     前提。
   - static gate → PASS；full suite → **3094 passed, 0 failed in 601.23s**，
     且已確認套件開跑後工作樹未再變動（上一包正是在這裡出錯）。

**Outcome**

- Phase 3 的 plan 綁定核心可用，且 `RB-DATASET-001` 的 D-5 拒絕行為已實作。
- **未完成**：`POST /execution-plans/preview`、`POST /runs/request`、
  `GET /runs/{id}` 三個端點，以及 approve-time 重驗與 Job 建立的接線。
  沒有任何端點會呼叫本模組，因此對執行路徑零影響。

**Next**

- WP-3B 續作：三個端點 + approve-time 重驗；完成後 `RB-DATASET-001` 才能
  真正關閉（拒絕行為要在 request 路徑上生效，不只是純函式可用）。

### WP-3B (complete) — Plan endpoints and approve-time re-verification

**Status:** `completed；RB-DATASET-001 已關閉`

**Task log**

1. `2026-07-28` — `resolver 與持久化`：`completed`
   - `Database.resolve_execution_plan_inputs()` 查出每個被選中的識別碼的
     當下狀態，交給純 planner 判斷。刻意放在 db 層而非 planner 裡，讓
     planner 保持零 I/O——它產生的每一個拒絕都能在沒有資料庫的情況下重現。
   - `PlanDraft` 改為**顯式攜帶** `dataset_none`，不從「snapshot 為 null」
     反推：digest 涵蓋該欄位，持久化時必須原樣重現而非猜測。
2. `2026-07-28` — `plan_run approval kind`：`completed`
   - payload 只有 `{plan_id, plan_digest, project_name}` 三個鍵，形狀不符
     即拒絕——approval **無法夾帶** plan 未釘住的指令或目標。
   - 不加入 `PINNED_EXECUTION_CONTRACTS`（那會強制套用 enqueue 的 payload
     形狀），改用獨立驗證分支。自動核准白名單仍恰好是 `enqueue|stop`。
3. `2026-07-28` — `三個端點`：`completed`
   - `POST /projects/{name}/execution-plans/preview`：純讀，不建立任何東西。
     測試直接比對呼叫前後的 plan/approval 列數。
   - `POST /projects/{name}/runs/request`：**重新推導** plan，不信任 client
     算出的任何 digest；未 ready 即回 400 且**不持久化任何東西**，不留下
     沒人能處理的孤兒 plan 或 approval。
   - `GET /runs/{plan_id}`：顯示 plan 與其 approval。
   - 三條路由都登錄 authorization catalog；preview 是 POST 只因為要帶
     body，它不建立東西所以只掛 `PROJECT_VIEW`。路由計數 119 → 122。
4. `2026-07-28` — `approve-time 重驗`：`completed`
   - `reverify_persisted_plan()` 從**既存欄位**重建 canonical body 再重算
     digest——plan 只存 `command_sha256` 而非指令原文，存兩份會產生可能與
     digest 漂移的東西。
   - 任一綁定 revision 在等待期間變動（version 消失、profile 封存、snapshot
     未發布、target 失去 approved、指令變危險），approval 即被 **rejected**
     並寫稽核，而不是靜默執行較新的輸入。
5. `2026-07-28` — `驗證`：`completed`
   - `tests/test_execution_plan.py` 27 tests + `tests/test_execution_plan_api.py`
     7 tests；static gate → PASS；full suite → **3110 passed**。
   - 改 ledger 後**重跑**完整套件（見下）。

**Outcome**

- **`RB-DATASET-001` 已關閉**：D-5 的拒絕行為現在在使用者真正會走到的
  request 路徑上生效，並以真實 app 端到端驗證。純函式能拒絕不算數。
- **未連上派工**：核准後的 plan 目前不會建立 Job，那是 WP-3C。

**Next**

- `WP-3C`：Codex promotion 與 Project page E2E，需要 `DG-CODE-PROMOTE` 裁定。

### Phase 4 (partial) — Runnable Node Agent daemon

**Status:** `daemon 可執行；啟用仍受 DG-NODE-V2 阻擋`

**Task log**

1. `2026-07-28` — `agent/__main__.py`：`completed`
   - Phase 0 的能力稽核記錄「此檔不存在」，這正是 `node_daemon` 一直是
     `implemented=no` 的原因——systemd 模板的 `ExecStart` 指向一個不存在的
     東西。現在補上：`python -m agent --check` 在**零網路 I/O** 下驗證設定、
     匯入、工作目錄可寫與非 root；`python -m agent` 跑 poll/ack/launch/
     heartbeat 迴圈。
   - `--check` 在偵測到以 root 執行時**直接 FAIL**而非警告：以 root 跑的
     agent 會瓦解整個 Node 設計所依賴的隔離。
   - 憑證只從環境讀入；`AgentConfig.__repr__` 明確 redact token，避免它進入
     traceback 或除錯工作階段。設定錯誤訊息只點名變數、不引用其值。
   - 明文 control-plane URL 一律拒絕（localhost 除外供開發），否則 node
     token 會裸奔在線路上。
2. `2026-07-28` — `修正一個會啟動真實行程的測試缺陷`：`completed`
   - `runner.launch()` 的 `spawn=subprocess.Popen` 是**預設參數**，在函式
     定義時綁定，事後 monkeypatch 模組屬性無效——第一版測試因此真的
     spawn 了 `/bin/bash`（stdout 出現工作負載的輸出）。
   - daemon 補上可注入的 `spawn` seam。這不只是測試便利：沒有它，任何
     launch 路徑的測試都會在開發機上開真實行程。
3. `2026-07-28` — `三處誠實邊界的遷移`：`completed`
   - `scripts/node_primitives_smoke.py`、`tests/test_node_primitives_smoke.py`、
     `tests/test_document_authority.py` 與 README 原本都釘住「daemon 不存在」。
     Phase 4 讓那句話變成假的，因此**邊界遷移而非刪除**：現在斷言 daemon
     存在、不匯入任何 control-plane 模組、不含任何 inbound primitive
     （bind/listen/HTTPServer/socketserver/uvicorn），且仍受 `DG-NODE-V2`
     與實機 canary 閘門阻擋。刪掉這些檢查會讓 agent 的隔離變成無人驗證。
4. `2026-07-28` — `驗證`：`completed`
   - `tests/test_node_agent_daemon.py` **23 tests**：憑證衛生、重啟後不重啟
     已 ack 的工作、reused/duplicate-ack 不二次啟動、指令只走檔案、ack 先於
     spawn 落地、ack response-loss journal/current recovery、backoff/jitter、
     stop receipt/process-group signal、control plane 不可達不算失敗、關機不動
     既有工作、模組不開 listener。
   - static gate → PASS；full suite → 見下。

**Outcome**

- `node_daemon` 由 `implemented=no` 變 `yes`。**但 Node 仍然不能接工作**：
  per-node 啟用需 `DG-NODE-CANARY` 實機證據，split assignment flags 維持關閉。
- 可執行的 daemon ≠ 可用的 Node。ledger 的 `deployed`/`canary-proven`/
  `production-ready` 全部維持 `no`。

**Next**

- `DG-NODE-V2` 已於 2026-07-28 核准；下一步是依其 contract 取得
  `DG-NODE-CANARY` 實機證據，再進 Phase 5。

### Phase 6 historical slice — Health, readiness and restore drill

**Status:** `不需 gate 的維運面已完成；production-ready 宣告仍待 DG-OPS-SLO`

**Task log**

1. `2026-07-28` — `liveness / readiness`：`completed`
   - `GET /healthz` 刻意**不碰資料庫**：一個會因為慢查詢而失敗的 liveness
     probe，只會重啟一個「唯一問題是查詢慢」的行程。
   - `GET /readyz` 檢查 schema 完整性（跨世代各取一張表，半套用的 migration
     報 not-ready 而非綠燈）、loop 新鮮度、state 路徑可寫、leader 狀態。
   - loop 新鮮度以**最後完成的迭代**計時，因此掛住的 loop 會過期而不是看起來
     健康——這正是 §11.2 要求的「不能讓 `/` 繼續假綠」。門檻是三個 interval：
     漏一拍可能是排程抖動，漏三拍不是。
   - **非 leader 回 ready**：它能服務讀取與核准，拒絕它會把健康的副本踢出
     輪替。
2. `2026-07-28` — `一個我刻意不跨的邊界`：`noted`
   - health probe 通常做成不需認證，但那要修改 `_AUTH_EXEMPT_ROUTES`，
     而該集合是 `INV-APPROVAL-5` 的受保護邊界、且被 static gate 逐字釘住。
     **健康檢查不該悄悄拓寬認證豁免**，因此兩個端點都是 authenticated
     （`PLATFORM_VIEW`）。若需要無認證探針，那是一次獨立的 invariant 修訂。
3. `2026-07-28` — `restore drill`：`completed`
   - 新增 `scripts/restore_drill.py`：對**副本**還原、跑 integrity check、
     與線上資料庫逐表比對列數、量測實際還原耗時（RPO/RTO 必須基於量測而非
     猜測）。
   - 測試發現實質缺陷：嚴重損毀的檔案會讓 `PRAGMA integrity_check` **拋
     例外**，操作者拿到 traceback 而不是判定。已改為回報 FAIL。
   - 另外標記「還原後列數**多於**來源」——那是唯一絕不良性的漂移方向，
     代表這份備份不是這個資料庫的。
4. `2026-07-28` — `剩餘 gate 起草`：`completed`
   - `docs/DG_NODE_V2_DECISION.md`：server-selected lease（現行 v1 由 agent
     自報 `job_id`，這顛倒了信任關係）、current-attempt recovery、Node
     terminal 收斂 canonical Job、staged rotation、退役與緊急撤權分流。
   - `docs/DG_OPS_SLO_DECISION.md`：RPO 24h / RTO 4h、備份逾時告警、季度
     drill、retention，以及**什麼證據才配宣稱 production-ready**。
   - 起草不是核准。三份 gate（含先前的 `DG-CODE-PROMOTE`）都等你裁定。
5. `2026-07-28` — `驗證`：`completed`
   - 13 個新測試；static gate → PASS；full suite → **3141 passed**。

**Outcome**

- Phase 6 中不需要 gate、不需要真實機器的部分已完成。
- **仍不可宣稱 production-ready**：那需要 `DG-OPS-SLO` 裁定，以及部署與
  canary 證據——兩者本 session 都無法產生。

**Next**

- 後續狀態更新：`DG-CODE-PROMOTE` 與 `DG-NODE-V2` 已核准且完成本機實作；
  目前只剩 `DG-OPS-SLO` 待裁定，且裁定本身仍不等於 production-ready。
- 需要真實機器：WP-2D canary、Phase 5 兩節點 7 天 canary。

### WP-3C — Code promotion（DG-CODE-PROMOTE-v1 核准後實作）

**Status:** `completed；Phase 3 閉環在程式碼層面完整`

**前情**：本包曾在裁定前實作過一次，被
`test_all_three_d4_kinds_are_deliberately_absent` 擋下並**整包 revert**。
該測試把 `engineering_task_promote` 釘為刻意不存在，理由是「沒有任何型別或
流程定義其語意」，並要求「要做的話必須先有具名裁定」。草稿不是裁定，唯一
能讓實作通過的方法是改那個測試，那是被禁止的。使用者於 2026-07-28 具名
核准後才重做。

**Task log**

1. `2026-07-28` — `裁定記錄`：`completed`
   - reviewed-draft `510d4075…` 對應 commit `4abd84e`，digest 可驗證。
     P-1…P-5 全部採用建議值。
2. `2026-07-28` — `promotion 契約`：`completed`
   - payload 恰好五個鍵、只有識別碼與 digest。bundle 路徑由 engineering
     task id **推導**而非取自 payload——payload 裡的路徑會是 approve 時
     可被重新詮釋的值。
   - commit 必須是 40 位小寫 hex、digest 必須是 64 位；大寫 commit 也拒絕。
3. `2026-07-28` — `approve-time 重驗`：`completed`
   - 重算 bundle SHA-256 並比對。**request 與 approve 之間重新產生的 bundle
     是不同的 artifact**，即使 diff 完全一樣——可重現性的宣稱是關於位元組
     的。測試直接驗證這個情境：rebuild 後核准被 rejected 且零版本匯入。
   - ProjectVersion 列在任何 hub reference 發布**之前**建立：崩潰留下的是
     沒被引用的版本（無害、看得見），而不是指向不存在版本的 dangling
     pointer。
4. `2026-07-28` — `邊界釘選遷移（依裁定）`：`completed`
   - `test_all_three_d4_kinds_are_deliberately_absent` 拆成兩個測試：
     `engineering_task_pr`／`_finalize` **維持**刻意不存在；
     `engineering_task_promote` 的釘選遷移為「存在**且永不自動核准**」。
     這是依裁定遷移邊界，不是為了讓實作通過而放寬——新測試比舊的更嚴格，
     因為它額外斷言了 P-1。
5. `2026-07-28` — `驗證`：`completed`
   - `tests/test_code_promotion.py` **15 tests**；static gate → PASS；
     full suite → **3157 passed, 0 failed in 609.69s**，且確認套件開跑後
     工作樹未再變動。

**Outcome**

- **Phase 3 的閉環在程式碼層面完整**：code（promotion）、data（snapshot）、
  plan（immutable binding）三者都可釘住，且只有經人工核准 promote 的版本
  能支撐 reproducible run。
- **仍未接上執行**：核准的 plan 不會建立 Job。那是 Phase 3 剩下的最後一段。

**Next**

- 把核准的 plan 接上 Job 建立（plan §8.4 的 approve-time「建立/連結
  canonical Job」），Phase 3 才算端到端可用。

### Phase 3 closing — an approved plan materializes a Job

**Status:** `completed；Phase 3 端到端可用`

**Task log**

1. `2026-07-28` — `plan 儲存指令原文`：`completed`
   - 先前 plan 只存 `command_sha256`，但 materialize 需要位元組。改為在 plan
     上存**單一副本**並在 insert 時驗 `sha256(command) == command_sha256`
     ——一份副本加上被檢查的 digest，不可能漂移；先前的顧慮是「存兩份」。
   - `command` 納入不可變 trigger；`job_id` 刻意**不**納入，因為
     materialize 對它寫入一次，那是對已核准 plan 唯一正當的寫入。
2. `2026-07-28` — `materialize_plan_job()`：`completed`
   - 三個性質在同一 transaction 保證：**一個 plan 至多一個 Job**（重複核准
     回傳既有 Job，不會讓已審閱的 plan 變成兩次執行）；**target 由 plan 自己
     的 revision 釘住**（scheduler 只決定「何時」，不決定「在哪」，plan §8.4）；
     Job 帶著 plan 的 approval 與 digest，可回溯到人核准的究竟是什麼。
3. `2026-07-28` — `抓到兩個實質 bug`：`completed`
   - **`is_dangerous()` 回傳 tuple 而非 bool**，我在 WP-3B 寫的
     `bool(is_dangerous(...))` 對非空 tuple 永遠為 `True`——**每一個 run
     request 都會被判定為危險指令而失敗**。純函式測試抓不到，因為它們注入
     `ResolvedInputs`、不走真實 resolver；是端到端測試抓到的。
   - Job 的 `approved_payload_sha256` 必須等於**該 approval 的** payload
     digest（既有 linkage trigger 檢查的是這個），我原本填成 plan digest。
     plan digest 仍可經 `execution_plans.job_id` 回溯，可追溯性不受損。
4. `2026-07-28` — `驗證`：`completed`
   - 端到端測試涵蓋：核准後出現 queued 且 pin 正確的 Job、重複核准不產生
     第二個 Job、被 reject 的 plan 零 Job 且 `job_id` 維持 `NULL`。
   - static gate → PASS；full suite → **3162 passed, 0 failed in 616.35s**，
     並確認套件開跑後工作樹未變動。

**Outcome — Phase 3 完成**

計劃書自稱的核心產品里程碑達成（在程式碼與測試層面）：

```
code  ✓ 只有人工核准 promote 的 ProjectVersion 能被綁定
data  ✓ content-addressed snapshot，或明確的 dataset_none
plan  ✓ 不可變綁定 + approve-time 重驗
Job   ✓ 核准後產生，pin 在 plan 選定的目標上
```

**仍然沒有通電**：這條路徑不會被 scheduler 派出去執行，因為
`EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED` 預設關閉，且該 flag 需要 WP-2D canary
才能開。Phase 3 證明的是「可以產生一個可追溯的 Job」，不是「這個 Job 會被
可靠地執行」——後者是 Phase 2 canary 的職責。

### WP-4A — Server-selected leases（DG-NODE-V2-v1 核准後實作）

**Status:** `completed；啟用仍需 DG-NODE-CANARY`

**Task log**

1. `2026-07-28` — `裁定記錄`：`completed`
   - reviewed-draft `870ba081…` 對應 commit `3d91686`，可驗證。
     N-1…N-5 全部採用建議值。
2. `2026-07-28` — `N-1 server-selected lease`：`completed`
   - `POST /node-agent/poll` 不再接受 `job_id`。`select_job_for_node()` 由
     control plane 以 FIFO 決定性選擇，套用與 SSH 路徑相同的資格規則
     （pin_server 相符、canary 資格、dispatchable）。
   - `NodePollRequest` 刻意用 `extra="forbid"`（偏離其他 node model 的
     `extra="ignore"` 慣例）：仍送 `job_id` 的 v1 agent 會被**拒絕**而非
     默默忽略——默默忽略會讓人以為舊行為仍然有效。
3. `2026-07-28` — `抓到一個我自己造成的實質 bug`：`completed`
   - 初版把「選新工作」放在最前面，但 server-side 選擇只看 `queued` job
     ——**已經領走工作的節點在下一次 poll 時會選不到任何東西，等於弄丟
     自己的 attempt**。既有的 `test_duplicate_poll_returns_same_attempt`
     直接抓到。
   - 修正為：先查該 node 是否已持有非終態 attempt，有就回傳它，沒有才選新
     工作。這正是 gate 說的「每 node 單一 active attempt」該優先於選新工作。
4. `2026-07-28` — `N-2 current-attempt recovery`：`completed`
   - 新增 `POST /node-agent/current-attempt`：重啟的 agent **詢問**而非推論
     自己擁有什麼。agent 永遠不需要判斷一個已 ack 的 attempt 是否可放棄
     ——那個判斷不論往哪個方向錯，都是重複執行或偽造終態。
   - 該路由登錄為 node channel，由 node 憑證驗證，不掛任何 actor action。
5. `2026-07-28` — `Node terminal 收斂 canonical Job`：`completed`
   - v1 只關閉 `node_attempts`，Job 可能永遠停在 `running`。新增
     `apply_node_terminal_to_job()`，守衛與 SSH 投影相同：終態必須與 node
     attempt 自身記錄的狀態相符，不得憑空產生；已非 `running` 的 Job 不動
     （stop 或先前的收斂已經決定了它）。
6. `2026-07-28` — `v1 測試遷移`：`completed`
   - 既有 poll 測試改為 v2 契約。`test_poll_for_unknown_job_is_404` 在 v2
     失去意義（沒有 job_id 可以是未知的），改為
     `test_poll_rejects_an_agent_that_still_names_a_job`；
     `test_ineligible_job_is_refused_at_the_poll_endpoint` 改為斷言
     **選擇器**拒絕，而非「被指名的 job 被拒絕」。
7. `2026-07-28` — `驗證`：`completed`
   - `tests/test_node_v2_lease.py` **14 tests**；既有 node 套件 114 passed；
     static gate → PASS；full suite → 見下。

**Outcome**

- Node v2 的協議面完成。**但 Node 仍不能接真實工作**：
  `NODE_AGENT_V1_ENABLED` 維持關閉、沒有任何 node 被登記、實機啟用需
  `DG-NODE-CANARY`。

**Next**

- `DG-NODE-CANARY`（未起草）與 Phase 5 的兩節點 / 100 jobs / 7 天實機驗證。
  兩者都需要真實機器。

### WP-4B — Staged rotation, draining, and revocation

**Status:** `completed；啟用仍需 DG-NODE-CANARY`

**Task log**

1. `2026-07-28` — `N-4 staged rotation`：`completed`
   - `nodes` 增 `previous_secret_hash`／`previous_secret_expires_at`（additive
     雙軌遷移）。`rotate_node_credential(overlap_sec=N)` 保留舊 secret 至
     過期為止；不給 `overlap_sec` 則維持立即失效——那是緊急換鑰匙要的語意。
   - `authenticate_node()` 在重疊窗口內接受新舊兩把。**撤銷檢查放在最後且
     絕對優先**：重疊窗口保護的是 rotation，不是已撤銷的憑證。
   - 第二次 rotation 會丟棄第一次的 previous secret——不會累積一串仍被接受
     的舊 token。
2. `2026-07-28` — `N-3 draining 與 revocation 分流`：`completed`
   - `nodes.draining_at` + `set_node_draining()`。draining 的 node **保有身分
     與憑證**，只是不再被指派新工作——它還要能回報自己手上的工作。可逆。
   - revocation 立即生效，且**不把在途工作標成 failed**：撤銷憑證不是關於
     工作負載的證據，標 failed 等於捏造沒人觀察到的終態。測試直接斷言
     attempt 的 `terminal_at`/`exit_code` 維持 `NULL`、Job 狀態不變。
3. `2026-07-28` — `修正一個我造成的嚴重錯置`：`completed`
   - 我把 node 的三個新欄位加進了 **`Job.from_row`** 而不是 `Node.from_row`
     ——那會讓每次讀取 Job 都嘗試存取不存在的欄位。症狀是 rotation 後
     `previous_secret_hash` 讀不到；逐層追下去才發現插入點錯了。
4. `2026-07-28` — `驗證`：`completed`
   - `tests/test_node_credential_lifecycle.py` **13 tests**，含 legacy node
     列遷移不捏造 rotation 狀態；static gate → PASS；
     full suite → **3190 passed, 0 failed in 625.89s**。

**Outcome**

- WP-4B 完成。Node 的憑證生命週期在協議層完整：rotation 不再是停機，
  退役與撤權語意分離。
- **仍不能接真實工作**：`NODE_AGENT_V1_ENABLED` 關閉、無 node 登記、
  實機啟用需 `DG-NODE-CANARY`。

### Post-audit Node safety hardening（2026-08-02）

**Status:** `code-complete；default-off；未部署／未取代實機 gate`

這個工作包重新驗證使用者提出的 10 個風險；不是把 review 文字直接當成事實。
本機可重現證據確認其中 7 項成立、3 項為「v2 已安全但 legacy／邊界仍有缺口」。
修正遵守已核准的 `DG-NODE-V2-v1` 與 canonical invariants，沒有變更
unknown≠failed、approved payload immutable、SSH rollback 或 audit best-effort
語意。

**Task log**

1. `atomic claim + DB constraints`：`completed`
   - legacy `lease_job_for_node()` 不再 read-then-insert；expiry、Node/Job/digest
     重驗、idempotent reuse 與 INSERT 在同一個 `BEGIN IMMEDIATE`。
   - partial unique indexes 保證每 Job、每 Node 一個 active `node_attempts`；
     linked v2 另保證每 server 一個 active Node `execution_attempts`。
   - fresh schema 新增 `job_id/node_id/execution_attempt_id/attempt_id` native FK
     與 Node/attempt status CHECK。SQLite 無法 additive ALTER constraint，故舊
     schema 保留歷史列、以 trigger 阻擋所有未來 orphan/invalid writes；不補造
     missing node、revision 或歷史資料。
   - 兩個獨立 SQLite connection 的並行碰撞測試涵蓋 same-Job 與 same-Node，
     各自都只留下 1 個 active attempt。
2. `restart terminal + process identity`：`completed`
   - 新增 `agent/supervisor.py`。daemon 在 launch intent fsync 後啟動固定 argv
     supervisor；supervisor 驗證 `cmd.sh` digest、移除 workload 環境中的 Node
     token/activation nonce，並原子/fsync 寫 `supervisor.json`、`terminal.json`。
   - daemon 只從 matching attempt/digest/boot-id/`/proc` start-time 的 sentinel
     接受 terminal。測試強制 `waitpid()` 回 `ChildProcessError`，仍能在重啟
     情境收斂 exit 7；不再要求舊 workload 是新 daemon 的 child。
   - stop 只對 boot/start-time matching supervisor 用 pidfd 發 signal；裸 PID、
     PID reuse 或不支援 pidfd 都 fail-closed。SIGUSR1 由 supervisor 對 workload
     group 做 SIGKILL escalation，保留 supervisor 寫 terminal 的機會。
     systemd template 加 `KillMode=process`，daemon restart 不殺 supervisor。
3. `atomic terminal + artifact batch`：`completed`
   - unlinked legacy terminal 與 canonical Job 在同一交易收斂；exact duplicate
     可修復既有半套 projection，conflicting terminal 409 且不覆寫 first result。
   - 故障注入讓 Job terminal UPDATE 中止，驗證 node row 也完整 rollback。
   - artifact ownership 重驗與整批 upsert 改為一個交易；第二筆注入失敗時第一
     筆不殘留。同一 request 的 duplicate path 也 fail-closed。
4. `wire/API correctness`：`completed`
   - ExecutionPlan digest 不符改回封閉且阻擋性的
     `plan_digest_mismatch`，不再錯報 `plan_ready`。
   - Node request model 全部 `extra="forbid"`，並限制 attempt/digest、agent
     version、artifact count/path/size、exit code 與 log-tail UTF-8 bytes。
   - 高頻 Node sync routes 交給 FastAPI threadpool；auth middleware、terminal
     transaction、completion claim 與相關 DB lookup 使用 AppState tracked
     executor，不阻塞 event loop。
5. `audit failure visibility`：`completed with explicit residual`
   - `OSError` 仍不得回滾已授權 domain action（`INV-AUDIT-2` 未變），但不再
     silent pass：寫 error log，並在 `/operations/metrics.audit` 暴露 attempts、
     failures、consecutive failures、最後成功／失敗時間與錯誤 category。
   - 這是 process-local visibility，**不是 durable audit outbox**；若要把 audit
     改成 fail-closed 或交易式 outbox，仍需具名裁定，不能在本修補偷改。
6. `verification`：`completed`
   - 新增 `tests/test_node_safety_hardening.py` 並加入 static release gate pin。
   - targeted Node/execution/audit/health suite：**348 passed**。
   - static invariant gate：**PASS**。
   - full offline suite：**3366 passed, 0 failed in 775.04s**。
   - 測試只使用暫存 DB／本機 subprocess；未連 worker、未讀 credential、未改
     runtime `jobqueue.db`／`audit.jsonl`／`servers.yaml`。

**Outcome / remaining gate**

- 使用者列出的 10 項程式碼風險已修正並有回歸測試；沒有已知架構衝突。
- 這仍只把 Node foundation 從「有致命競態／重啟缺口」提升為「可進實機
  canary 的候選」。ledger 的 `deployed`、`canary-proven`、`production-ready`
  維持 `no`；`RB-NODE-001` 仍需 `DG-NODE-CANARY` 與 Phase 5 的
  2 nodes / 100 jobs / 7 days。
- SSH backend、所有 Node rollout flags 與現行服務都未啟用或重啟。
