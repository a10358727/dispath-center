# Audit evidence and adoption

The durable audit path is intentionally incremental. `audit_events` is the
transactional source of truth for migrated UoW mutations; `audit.jsonl` is an
asynchronous, at-least-once export and the compatibility sink for legacy
mutations. Existing JSONL lines are never backfilled into the ledger because
their actor, resource, and timestamp provenance cannot be reconstructed.

## Evidence contract

Schema migration 3 adds `hash_contract_version` to every durable event. New
rows use `durable-audit-v1`, whose SHA-256 envelope contains the contract
version, predecessor hash, and canonical UTF-8 JSON event bytes. Canonical JSON
uses sorted keys, compact separators, explicit `null` values, and UTC
millisecond timestamps. Rows created by the first PR-08 implementation use
the compatibility `durable-audit-v0` envelope and remain immutable.

Schema migration 4 adds the immutable Node artifact `kind` discriminator. Old
rows receive the neutral `file` classification without rewriting their size
or digest.

`created_at` is the database transaction timestamp used as the recorded-at
value; `occurred_at` is the API-compatible event timestamp. Sequence/event ID,
not a timestamp, is the durable cursor and chain order.

SQLite triggers reject every `UPDATE` and `DELETE` on `audit_events`. An append
reads the predecessor and writes the event plus its 1:1 export operation inside
the same `BEGIN IMMEDIATE` transaction. `audit_export_operations` has a unique
foreign key to its event, a compare-and-swap claim with a lease, bounded retry
attempts, sanitized error categories, and an explicit dead-letter state.
The compatibility state names `failed` and `exported` mean retry-wait and
delivered respectively; they remain stable for existing operators.

JSONL export is at-least-once. A crash after append and before marking the
operation exported can produce a duplicate line; every durable line carries
`event_id`, `event_sha256`, and `hash_contract_version`, and readers deduplicate
on the stable ID/hash pair. A partial final JSONL line is ignored by the
best-effort compatibility reader and does not erase the database event.

The database event remains authoritative; operational metrics expose the
export outbox's pending/processing/failed/dead-letter counts and the age of
the oldest active processing lease. The read-only status command and
`/operations/metrics` share the same projection: any backlog is
`status=attention`, while an exhausted dead-letter row activates a critical
`dead_letter_present` alert signal. This is local evidence only; no external
notifier, SLO, or off-host anchor is implied.

For an offline or operator-run point-in-time gate, `scripts/audit_export_status.py`
opens the SQLite file read-only and reports the same counts. With
`--require-clear` it exits non-zero when backlog or dead-letter rows exist;
this is evidence collection, not a production alert/readiness threshold.

The source outbox drain is an operator-approved state change. Before running
`dispatch db audit-export` against the source database, capture an online
backup, record a `--require-clear --json` pre-check, use an owner-scoped output
path, and retain a post-check showing `status=clear`. A copied-database drain
proves only that the command works; it does not authorize or clear the source
backlog. Failed drains leave the source state for the existing lease/CAS retry
path, and dead-letter rows still require the explicit `audit-replay` command
with operator identity and reason code.

Dead-letter rows are not automatically replayed. An operator must invoke the
`dispatch db audit-replay` command with an operation ID, operator identity, and
reason code. The replay request itself is a new durable audit event.

## API evidence quality

`GET /events` and `GET /audit` preserve the existing list response shape. Each
row now includes:

```json
{
  "source": "durable_db",
  "durability": "transactional"
}
```

Legacy rows are marked `source: "legacy_jsonl"` and
`durability: "best_effort"`. The `X-Audit-Coverage` response header and each
row's `audit_coverage` value expose the current partial-adoption catalog.
Cursor pagination uses durable event IDs; legacy rows are included only on the
initial page because they have no durable cursor provenance.

The machine-readable catalog is `app.audit_adoption.AUDIT_ADOPTION`. It is a
typed inventory covering identity, approval, server/node, execution,
project/dataset, and engineering mutation families. Durable entries are
transactional; legacy entries carry an explicit owner and planned roadmap
slice. It also inventories every literal `append_audit()` action still emitted
by the application, including operational/read-only compatibility summaries;
this inventory does not upgrade those paths to durable state. Closed dynamic
writers used by `approval.kind`, server `action_name`, and authorization-shadow
`item.audit_action` are expanded from their source contracts and must resolve to
the same catalog owner/issue; open dynamic runtime strings are not guessed. The
generic
`reject()` route is covered by the transactional
`approval_decided` event; approval create/decide/approve/reject are all
catalogued as durable. Existing request routes still emit the historical
`approval_requested` JSONL summary, tracked separately as
`approval.compatibility`; it is not treated as a second durable decision.
The coverage projection also includes `required_durable_actions`,
`required_legacy_actions`, and `required_missing`. These fields scope the
result to the roadmap's required mutation-family inventory, so an explicitly
tracked compatibility summary is distinguishable from an omitted mutation
family. They do not change the `partial` mode or promote legacy JSONL output.
Ordinary enqueue, including its
setup/sync/bundle graph, now uses `execution_job_materialized` with all Job
inserts and the decision in one UoW; its unpinned payload is explicitly marked
legacy in each event. Dataset prewarm sync materialization uses the same
transaction-bound event and no longer routes through the legacy enqueue writer.
The standalone enqueue compatibility wrapper still emits a compatibility JSONL
line, but its Job insert also writes the durable materialization event; the
remaining legacy action is tracked by `execution.compatibility`.
Policy-scoped auto-placement decisions use the transaction-bound
`auto_placement_policy_decision` event.
The `LEGACY_AUDIT_JSONL_ENABLED` setting is a typed, default-on compatibility
switch for already-durable standalone-enqueue, unbound-coding-task, and
unpinned-server compatibility summaries. Setting it to `false` suppresses only
those JSONL lines; durable database events and rejection/error summaries remain
enabled. This is a reversible local retirement control, not evidence that the
remaining engineering/execution/server compatibility families have been
migrated or that
external export and retention have been accepted.
Approval-gated Run Profile and Dispatch Policy revision inserts now write a
bounded, hash-chained `run_profile_revision_created` or
`dispatch_policy_revision_created` event, update the immutable revision row, and
commit the typed approval decision in one SQLite transaction. Their historical
route-specific JSONL summaries remain explicit `run_profile.compatibility` and
`dispatch_policy.compatibility` entries until a separately reviewed retirement.
Experiment-record create/update/delete mutations now use the same transaction
boundary across the HTTP and Agent-tool paths. Durable events retain only the
record/project identity, lifecycle, author/kind or changed-field metadata, and
link-presence booleans; Markdown content and titles stay in the record row.
Approved project-membership grant/update/remove decisions now use the same
transaction boundary for the membership row, the bounded identity event, and
`approval_decided`; the identity event carries the approval correlation but no
credential material. Same-role and already-absent decisions remain idempotent,
and append-failure tests leave both the membership and pending approval
unchanged.
Approved service-account creation and service-token issue/revoke decisions use
the same boundary for the identity mutation, its bounded lifecycle event, and
`approval_decided`. The service identity event carries the approval correlation
but excludes bearer tokens, secret hashes, labels, and scope values. Disabled
service accounts remain ineligible for new token issuance, already-revoked
tokens remain idempotent, and append-failure tests roll back the identity row or
revocation while leaving the approval pending.
Remote dataset-cache reconciliation now uses one transaction for all observed
adds/removes, their row-level events, and a count-only `dataset_cache_reconciled`
event. The existing `cache_reconcile` JSONL summary remains an explicit,
default-on compatibility projection controlled by
`LEGACY_AUDIT_JSONL_ENABLED`; it is not authoritative. An append failure rolls
back the whole observation, while an unchanged observation emits no event.
Read-only project-instance reconcile now uses the same boundary for each state
or git-snapshot change and a bounded `project_instance_reconciled` event. The
event excludes paths, command output, and exception text; unchanged sightings
only refresh `last_seen`, and the existing `instance_reconcile` JSONL summary
remains compatibility evidence.
Project candidate upsert/status and project-instance create/upsert now also
use bounded lifecycle events (`project_candidate_created`,
`project_candidate_updated`, `project_candidate_status_changed`,
`project_instance_created`, and `project_instance_updated`); unchanged scans
remain quiet and remote paths/content stay out of durable parameters. Approved
`import_project` and `ignore_project_candidate` decisions join their row
mutations, lifecycle events, and `approval_decided` in one transaction, while
the established JSONL summaries remain compatibility evidence.
Inventory scan candidate upserts now use the same batch boundary after the
read-only SSH scan completes, so a durable append failure rolls back the whole
observation rather than leaving a partial candidate set. The nested-candidate
ignore approval likewise rechecks and updates its candidate IDs in one
transaction; already-processed IDs remain explicit skips and the
`candidates_ignore_nested` JSONL line remains a compatibility summary.
Canonical ProjectVersion creation also emits `project_version_created` in the
same transaction as the immutable version row. Existing commit identities are
returned unchanged without a second event; deploy-created versions carry the
approval correlation, while version metadata content remains outside durable
parameters.
Approval-gated `git_init` now records a payload-digest-bound
`project_git_init_intent` before its first mutating SSH command. Applied and
size-guard-rolled-back outcomes commit the instance git projection,
`project_git_init_outcome`, and `approval_decided` together; an SSH exception
is recorded as `unknown` while the approval stays pending for read-only
reconcile. The established `git_init` JSONL line remains compatibility evidence.
Project deploy follows the same boundary: `project_deploy_intent` is written
before local bundle/rsync/clone effects; applied instance/version projection,
`project_deploy_outcome`, and `approval_decided` commit together. Known command
failures retain the legacy rejected summary, while response loss stays pending
and is finalized only from read-only target HEAD/branch evidence.
Approval-gated `apply_patch` now uses the same pattern at its branch mutation
boundary: `project_apply_patch_intent` pins the approval payload digest,
instance, original branch, and generated branch before checkout. A successful
commit appends `project_apply_patch_outcome` and `approval_decided` in one
transaction; append failure or SSH response loss leaves the approval pending
with a bounded `unknown` outcome. Retries inspect only the pinned branch head
and never replay checkout, apply, or commit. The legacy full-diff JSONL line is
kept only as a compatibility summary; durable parameters omit the diff and
remote output.
Direct `hub_sync` now records `project_hub_sync_intent` before worker bundle
creation/rsync/fetch. Its successful `ProjectVersion` insert and
`project_hub_sync_outcome` commit together; command failures are bounded as
`failed`, while transport loss is `unknown`. Reusing an operation id can
converge the same intent without duplicating an immutable version, and the
legacy `hub_sync` JSONL line remains compatibility evidence.
Approval-gated `server_bootstrap` now records `server_bootstrap_intent` before
script upload/execution. The report row, `server_bootstrap_outcome`, and
`approval_decided` commit together; response loss remains `unknown`/pending and
retries refuse to execute the script again. Durable parameters contain only
target and script/payload digests, not key paths, report content, or capability
output.
The D-5 attempt filesystem preflight now records the exact revision-scoped
observation and `server_attempt_backend_preflight_recorded` in the same
transaction. Its bounded parameters contain no remote stdout, key path, or
exception text; the historical `server_attempt_backend_preflight` JSONL line
remains compatibility evidence.
Coding-run cleanup now records `engineering_task_cleanup_intent` before the
remote task-directory deletion and commits the local worktree projection with
`engineering_task_cleanup_outcome`. A response-loss or finalization failure is
`unknown` and blocks automatic replay; the intent digest binds the approved
cleanup contract without storing workspace/source paths.
The legacy queued-Job cancel path now also commits an
`execution_job_cancelled` event with the status transition; command text is not
copied into the ledger, and an append failure leaves the Job queued.
Legacy reconcile terminal (`done`/`failed`), interrupted requeue, and dependency
blocked transitions now emit bounded `execution_job_terminal_recorded`,
`execution_job_requeued`, or `execution_job_blocked` events in the same Job UoW.
Log tails, commands, and dependency payloads stay outside durable parameters;
`requeue_blocked` remains a separately tracked compatibility error summary.
Stalled detection also commits each `stalled_suspect` transition as
`execution_job_stall_state_recorded` with a bounded `stalled`/`cleared` reason;
unchanged observations are idempotently quiet. The durable append occurs before
the notification callback and the legacy `stall_suspect` summary, so an append
failure rolls the flag/log-size projection back without emitting a misleading
notification.
The retained unbound coding-run result collector also commits its `coding_runs`
projection with a bounded `coding_run_result_recorded` event. A durable append
failure rolls the result update back; the older `coding_finished` JSONL record
remains an explicit compatibility summary. Bound Engineering Task results keep
their existing `engineering_task_result_recorded` event.
`validate_audit_catalog()` is the CI gate for missing owners, missing legacy
tracking, duplicate emitted actions, and omitted required families. New
mutation slices must add a catalog entry and must not double-write a durable
action to the legacy JSONL writer.

CI runs `scripts/audit_adoption_gate.py`. It validates the catalog and scans
literal `append_audit()` calls for both unclassified actions and durable action
names, so the boundary fails closed when a new legacy action bypasses the
inventory or a migrated action is accidentally routed through the compatibility
writer. Dynamic action values remain subject to domain tests and explicit
catalog ownership.

Approval creation and status decisions are the first migrated high-risk
mutations. The approval row and its `approval_created` event commit together;
the payload itself is not copied into audit parameters. The
`approved`/`rejected` status update and its `approval_decided` event (including
actor, mechanism, approval resource, and export intent) commit in one SQLite
transaction. The generic `reject()` route no longer emits a duplicate JSONL
line. Ordinary enqueue also commits its complete Job graph and bounded
`execution_job_materialized` events in that transaction; a durable append
failure leaves every Job and the approval pending. Specialized enqueue remains
an explicit compatibility summary path. Dataset prewarm commits its sync Job,
approval decision, and materialization event in the same UoW; policy-scoped
auto-placement decisions are durable in that same UoW.
Pinned canary, worker-validation, execution-plan materialization, and the
unbound coding-task Job materializer now rely on their durable UoWs directly.
ExecutionPlan request materialization commits the immutable plan row, pending
approval, `approval_created`, and bounded `execution_plan_materialized` evidence
as one transaction; its parameters contain only the command digest and binding
booleans, while `plan_run_request` remains a compatibility summary.
The unbound coding-task wrapper still emits its explicit legacy JSONL summary;
the other migrated paths do not emit a second approval/plan JSONL summary.
Run Profile and Dispatch Policy create/update/archive approvals likewise use a
single revision-plus-decision UoW; an injected durable-audit failure leaves both
the immutable revision and the approval pending.
Experiment-record writes also roll back the row when the durable append fails;
missing-record update/delete calls remain idempotent no-ops.
Queued Job cancellation uses the same rollback boundary while Engineering Task
owner Jobs continue to reject the generic cancellation path. The retained SSH
scheduler fallback also records its queued→running claim and exception requeue
as bounded `execution_job_dispatched`／`execution_job_dispatch_requeued` events
inside `update_job()`; local sync dispatch shares the contract and pre-dispatch
disk rejection uses the terminal event. The legacy `dispatch` JSONL summaries
remain compatibility evidence, and audit append failure rolls the state back
before remote work begins.

Identity service-account, service-token, membership, and session create/revoke
mutations now use the same transaction boundary. Events retain only actor and
resource identifiers, role/scope counts, and lifecycle results; bearer hashes,
session secrets, and token labels are never copied into durable audit params.

Node lifecycle row mutations and their lifecycle events use the same boundary
for enrollment, legacy and staged credential rotation/activation, drain state
changes, revocation, and routine retirement. Approval-gated enrollment,
rotation, drain/resume/retire, and revocation now also join the corresponding
`approval_decided` event in that transaction. Events retain only the target
node/server, bounded mode or count fields, and the approval correlation; raw
tokens, secret digests, activation nonces, and credential identifiers are
excluded. A durable append failure rolls the corresponding Node state change
back. Node attempt lease
events remain in the legacy migration slice; attempt creation itself now emits
`node_attempt_created` transactionally for both direct inserts and legacy poll
claims. Node terminal convergence now adds one `execution_terminal_recorded`
summary in the same transaction for both generic and legacy attempts; retries
remain idempotent and log tails stay out of durable parameters.
Immutable artifact reports now add one digest-bound
`execution_artifact_recorded` summary per distinct report. The durable event
contains only node/resource identifiers, count, kind list, and the report
digest; paths, file contents, and raw credentials remain outside the ledger.
Stop requests are also transactionally visible: a generic stop outbox intent
records `execution_stop_requested`, while Node request/ack transitions record
`execution_stop_requested` and `execution_stop_acknowledged` with bounded
job/node/channel metadata. The approval route commits `approval_decided` and
the approved stop request together; repeated request or acknowledgement
retries are idempotent, and a durable append failure rolls the state change
back. Stop command text is never copied into audit parameters.
Positive or controller-won launch resolution now records
`launch_resolution_recorded` in the same transaction as the immutable attempt
evidence and outbox transition. The envelope stores only the proof category
and resolution (`launcher_claimed`, `delivered`, or `not_transmitted`), never a
receipt, command, or remote output. A failed append leaves the launch claim
and operation state unchanged.
Collection completion records `execution_result_recorded` for both the generic
`collect` outbox and the Node terminal completion bundle. It stores only the
Job/attempt relationship and delivered/failed state; result payloads remain in
their immutable operation evidence. Collection retries use deterministic event
IDs and do not duplicate a committed outcome.
Coding-run creation records `run_created` transactionally with bounded project,
runner, binding, and status metadata. The instruction and other mutable run
payload fields remain in the run row and are not copied into the audit ledger;
an append failure rolls the new run back.
Project create/update/delete, dataset registry creation, card updates,
dataset-cache add/remove mutations, and sync verification emit bounded durable
envelopes. Project events retain only resource identity and field/count
metadata; dataset events exclude source paths, manifests, card text, and raw
SSH/manifest diagnostics. Cache registration, Job terminal status, and the
`dataset_sync_verification_recorded` event commit together; retries are
idempotent and an audit failure rolls the corresponding Job/cache mutation
back.
Dataset archive is not a public operation yet, while older registry
compatibility paths remain explicitly catalogued as legacy.
Immutable snapshot build approval and publish/abort transitions now
emit `approval_decided` and `project_snapshot_published` transactionally;
archive/legacy registry paths remain open.
Pinned server-config publication prepare, YAML transition, activation, and
exact rollback/recovery transitions now emit bounded operation-specific
durable events in the publication transaction. Unpinned legacy server YAML
paths do not create a revision, but now emit hash-bound durable
`server_legacy_mutation_intent` plus applied/failed outcome evidence before and
after the external write; their old JSONL summaries remain under
`server.compatibility`, separately from `server.publication`.
Engineering Task creation, approval/rejection, queued-attempt publication,
native retry/discard, coding-run result projection for bound and unbound runs, and
project-version promotion prepare/finalize/retire now emit bounded
transactional envelopes. The `engineering_task.mutate` catalog entry maps task
create/update events to the durable writer; native retry/discard have dedicated
route events and UoWs. The unbound coding-task Job materializer is durable; only
the older wrapper's JSONL summary remains a compatibility entry. Both result
events are tracked under `engineering_task.result`.

## Integrity limits and production gates

A hash chain proves internal consistency, not that an attacker could not
rewrite the entire SQLite file and recompute the chain. Production deployment
therefore still requires an external checkpoint anchor (for example, the last
event hash per day or segment in off-host immutable storage, with an
independent signature). Retention must archive segments with first/last hash,
the previous segment hash, checksum/signature, and policy metadata; deleting
individual chain rows is not supported.

Backup restore verifies SQLite integrity and the internal chain. The
`restore-verify --audit-anchor` path can also verify an independently signed
checkpoint's boundary and optional backup-manifest binding, but it does not
provision off-host immutable storage or manage signing-key rotation. Full-
domain audit adoption, approval and identity migration, and the formal Node
canary remain open review items.
