# SQLite migrations

The control plane keeps its existing SQLite facade, but schema changes now go
through the ordered ledger in `schema_migrations`. Version 1 is the additive
compatibility migration that covers the historical `ALTER TABLE` columns,
project UUID backfill, and supporting indexes. Version 2 adds the durable
`audit_events` ledger and its export outbox. Version 3 adds the hash contract
discriminator while preserving v0 hashes already written by the first audit
foundation. Version 4 adds the immutable Node artifact kind discriminator,
defaulting historical rows to `file`. Version 5 adds completed-result
`api_idempotency_keys` storage for Product API v2; it does not infer or
backfill historical HTTP requests. Version 6 adds independent
`project_role_bindings` for Product RBAC v2 and deterministically maps only
valid legacy membership evidence. Existing values are preserved; no approval,
revision, digest, execution, idempotency, or role-grant history is fabricated,
and legacy JSONL lines are not backfilled into the new hash chain. Version 7
adds the canonical Project Experience tables: `project_environments`,
`environment_revisions`, `run_profile_specs`, and
`project_default_revisions`. It does not infer Environment, typed spec, or
Defaults rows for an existing Project or legacy Run Profile. Version 8 adds
the six Dataset governance tables for Project-owned assets, exact
published-snapshot links, share offers, grants, immutable aliases, and
immutable lineage. It performs no ownership or asset backfill for legacy
registry or snapshot rows. Version 9 adds the immutable
`execution_plan_v2_specs` companion and the exact closed ExecutionPlan-v2
approval-to-Job pin mapping; it performs no plan, Job, approval, attempt,
Dataset, or legacy-contract backfill. Version 10 adds the per-Project
`ai_conversations` (one main conversation per Project) and
`ai_conversation_messages` tables (DG-CONVERSATION-V1 CV-1); SQLite is the
only conversation truth and no legacy chat history is backfilled. Version 11
adds the task-neutral `agent_sessions` table carrying `provider_id`
(DG-AGENT-SESSION-V1 E-3). Version 12 adds the additive
`agent_sessions.active_turn_no`/`active_turn_started_at` pair for
one-turn-at-a-time tracking. Version 13 adds `run_metrics` and
`run_metrics_collection` (DG-METRICS-CONTRACT v1); `jobs` is untouched and
metrics evidence never affects job status. Version 14 adds `experiments` and
`experiment_plan_members` (DG-EXPERIMENT-V1 EX-4); `execution_plans` and its
triggers are untouched. Version 15 adds the `experiment_plan_specs`
companion (DG-EXPERIMENT-V1 P2 note) mirroring `execution_plan_v2_specs`'s
column shape so N experiment members can share one `experiment_create_v2`
approval; it never modifies `execution_plan_v2_specs` or its triggers.

The checked-in target is `CURRENT_SCHEMA_VERSION = 15` in
`app/migrations.py` (that constant, not this document, is the version
authority); `schema_is_initialized()` fails closed until the required table
set for the checked-in version is present. Product RBAC v2 landed at schema
version 9 era; the RBAC evidence rules below are unchanged by versions
10–15.

## Version 6 legacy-role evidence

Version 6 maps a valid human legacy `admin` row to Owner, Operator, Reviewer,
and Dataset Manager; `operator` maps to Operator and `viewer` maps to Viewer.
For service or legacy actors, the migration filters out Owner and Reviewer.
Rows with an unknown role, actor type, missing referent, or missing/invalid
timezone-aware `updated_at` remain unmapped and make no invented fallback
claim.

Each initial mapped binding has
`grant_provenance=legacy_membership`, `grant_approval_id=NULL`, and copies the
legacy row's `updated_at` bytes into `granted_at`. Despite the target column
name, this timestamp proves only that the legacy row presented that role at
its last observed update time. It does **not** prove the role's original grant
time, a human approval time, a granting actor, or approval provenance.
Migration execution time and actor creation time are never substituted.

After migration, only the approved v1 membership decision transaction may
project a legacy write into legacy-provenance bindings because it has a real
approval ID and decision timestamp. The direct
`upsert_project_membership()`/`delete_project_membership()` database helpers
remain low-level legacy/test compatibility primitives; they are not Product
v2 mutation authority and never infer or repair v2 provenance. Product v2
role changes never write legacy memberships.

A non-human Owner or Reviewer can arise only from unsupported manual
corruption or older external data, not from migration v6 or the approved role
change path. Such a Project remains `needs_role_repair` and fail closed.
PR-02 deliberately provides no delete/repair API for that corrupt evidence;
remediation requires a separately approved, evidence-preserving decision.

## Version 7 Project Experience ownership

Version 7 is the single schema owner for Environment revisions, Run Profile
typed specs, and Project Defaults. Its four tables are additive and immutable:
update/delete triggers reject attempts to rewrite an approved revision or
logical Environment row. Foreign keys use `ON DELETE RESTRICT`; exact revision
and digest references remain durable even when the corresponding runtime flag
is disabled.

The pinned migration source checksum is
`ad848c611a6501ee6402fba790360723470eb4accd04a9144b6400c7afc5f80c`.
`run_profile_specs.project_id` is constrained against both the Run Profile and
the exact Environment revision. Project Defaults use one composite foreign key
over Project, Run Profile, spec digest, and Environment revision, so SQLite
itself rejects cross-Project or mixed-spec references rather than relying only
on application validation.

PR-04 Project bootstrap creates revision 1 rows only after a separate human
Platform Admin approves the immutable `project_bootstrap_v2` payload. Project,
role bindings, Environment, typed Run Profile spec, Defaults, approval decision,
durable audit, and HTTP idempotency completion commit in one transaction.
Existing Projects and raw Run Profiles are not backfilled and remain honestly
legacy. PR-05 and PR-06 extend behavior on these same tables; they do not create
a parallel schema. Once any revision in a logical `(project_id, name)` Run
Profile lineage has a typed spec, legacy raw update/archive requests and their
approve-time race revalidation fail closed until the Product v2 compiler owns
that lineage.

PR-05 standalone Environment create/update/archive approvals reuse these exact
`project_environments` and `environment_revisions` tables. Each accepted
change appends one immutable N+1 revision, retains the predecessor, and never
updates or deletes history. Archive is a terminal copied successor rather than
a destructive mutation. PR-05 deliberately changes neither the version 7
migration source nor its pinned checksum; no parallel table, backfill, or
fabricated host evidence is introduced.

PR-06 standalone Run Template create/adopt/update/archive proposals reuse
`run_profiles` plus the one-to-one `run_profile_specs` companion; standalone
Project Defaults create/update proposals append to `project_default_revisions`.
Adoption never rewrites or decorates the legacy raw predecessor. Every accepted
successor, approval decision, bounded durable audit event, and completed HTTP
idempotency identity commits atomically. PR-06 also leaves the version 7 source
and checksum unchanged and performs no float/data rewrite: historical
integer-only typed bootstrap digests retain their exact bytes.

## Version 8 Dataset governance ownership

Version 8 is the one additive schema owner for `dataset_assets`,
`dataset_asset_snapshots`, `dataset_share_offers`,
`project_dataset_grants`, `dataset_alias_revisions`, and
`dataset_lineage_edges`. The pinned migration source checksum is
`bcfaadfa86db2d8f3d8f79102cddc5ca7c2f3f8023762ca150f8175d64e18b81`.
Snapshot identifiers remain bounded opaque text and are never interpreted as
UUIDs. Composite foreign keys keep asset/project, offer/grant, exact snapshot,
alias, and lineage references aligned.

Assets, snapshot links, offers, alias revisions, and lineage edges are
update/delete immutable. Grants have independent UUIDs and allow only one
trigger-guarded transition from active to revoked; delete, unrevoke, and any
other update fail closed. Alias identity and revision uniqueness are scoped by
`(project_id, asset_id, alias_name)`; changes append revisions under a
serialized expected-head compare-and-swap. Each exact input/output snapshot
pair has at most one lineage row. Lineage insertion performs its reachability
check and insert in the same `BEGIN IMMEDIATE` transaction so reciprocal
concurrent inserts cannot both create a cycle.

Migration v8 does not inspect legacy Dataset registry ownership, assign an
owner, create an empty asset, or link an existing snapshot. Only the
digest-bound `dataset_asset_adoption_v2` two-person transaction may adopt an
already-published, currently-unlinked snapshot. Runtime rollback disables
`DATASET_ASSETS_V2_ENABLED` and preserves every additive row and approval/audit
record.

PR-08 Dataset Sharing is the behavioral owner of the existing
`dataset_share_offers` and `project_dataset_grants` tables. It intentionally
adds no Migration v9 and does not modify Migration v8 source or its pinned
checksum. Offer approval inserts the immutable offer; target acceptance inserts
the complete exact grant set in one `BEGIN IMMEDIATE` transaction; source
revoke or target unlink uses only the trigger-guarded active-to-revoked update.
Runtime rollback first disables `DATASET_SHARING_V2_ENABLED`, hiding target
projection while retaining every offer, grant, revocation, alias, approval,
idempotency row, and durable audit event.

PR-09 Dataset Publish is also a behavioral consumer of the existing snapshot
tables plus Migration v8. It adds no table, column, index, trigger, Migration
v9, or legacy `datasets` registry row, and does not modify either migration's
pinned bytes/checksum. One approved publish reserves a `building` snapshot;
after content-addressed filesystem publication, one SQLite transaction publishes
its shard/snapshot evidence and creates the exact Project asset link, optional
initial alias revision, optional Run lineage, and durable audit events. A failed
finalization rolls all of those rows back and leaves the same snapshot building
for exact-ID resume. Runtime rollback disables `DATASET_PUBLISH_V2_ENABLED` and
retains building/published evidence and safe filesystem blobs. PR-09 left
Migration v9 exclusively to PR-10; that migration is documented below.

## Version 9 ExecutionPlan v2 ownership

Version 9 is the additive schema owner for `execution_plan_v2_specs`. Its
pinned migration source checksum is
`04e120f943dc8fe5c77174dca5ad06b01f778fa1de80729790ee4c82cdc020d9`.
The one-to-one immutable companion stores the canonical review spec plus exact
ProjectVersion, optional Project Defaults, Run Profile, Environment, resolved
Dataset bindings, target revision/policy, checkout, resource, argv, observation,
command, requester, and approval evidence. Insert-time triggers require its
legacy `execution_plans` projection, current same-Project typed references,
active approved target, and pending immutable approval to agree. Update and
delete always fail closed.

The same migration transaction replaces `jobs_execution_pin_insert_guard` with
a closed mapping. Existing generic/v1 contracts continue to require approval
payload version equality with the Job semantic version. Only exact approval kind
`execution_plan_v2` permits envelope version
`execution-plan-v2-approval-v1` to create a Job whose semantic execution
version is `execution-plan-v2`. No prefix, payload inference, backfill, or
alternate pair is accepted. Boot-time post-migration trigger repair installs
the identical definition, so reopening cannot downgrade the guard.

PR-10 request materialization commits approval, plan, companion, durable audit,
and idempotency completion atomically. Approval revalidation commits one pinned
Job and its decision/audit evidence atomically. A migration failure rolls back
both companion DDL and trigger replacement with no version-9 ledger row; retry
is safe. Runtime rollback first disables `RUN_EXPERIENCE_V2_ENABLED` and retains
all additive evidence. Software unable to read schema version 9 requires a
verified pre-upgrade backup; there is no destructive down-migration.

The migration runner also mirrors the applied version in SQLite's
`PRAGMA user_version`, takes a process-level lock beside the database, and
wraps each migration step and ledger write in a transaction. A failed step can
be retried; its ledger row is not recorded.

## Version 21 server_observations device columns

`server_observation_device_columns` (2026-09-02) adds `devices_json` and
`executables_json` to `server_observations` when they are missing. The
DG-HARDWARE-EXECUTION v1 P1 packets had only added the columns to the legacy
column list that runs as migration 1, so a database already at version 20
(the personal pilot) never received them and `insert_server_observation()`
failed on every monitor tick. The step is idempotent: a fresh database whose
SCHEMA already carries the columns records the ledger row as a no-op.

Lesson recorded for future additive columns: once a database is past
migration 1, a new column needs its own versioned migration; editing the
`_*_COLUMN_MIGRATIONS` tuples only serves databases that have never opened.

## Version 22 hardware_images

`hardware_images` (2026-09-03, DG-HARDWARE-EXECUTION v1 P2, H-2/H-3) adds
`run_profile_specs.action_class` (closed vocabulary `compute`/`build`/`program`/
`power`/`hil_test`, default `compute`; existing specs and their digests are
untouched) and the `hardware_images` registry: one row per content digest of a
programmable image a `build` run produced (`build_plan_id` → `execution_plans`,
`job_id` → `jobs`, `sha256` UNIQUE, `known_good_marked_by_approval_id` only ever
set by a human decision). Image bytes live content-addressed under
`{local_home_dir}/images/{sha256}` on Server A. Every statement is idempotent
(`ADD COLUMN` guarded by `PRAGMA table_info`, `CREATE … IF NOT EXISTS`).

## Version 23 hardware_action_v2_triggers

`hardware_action_v2_triggers` (2026-09-03, DG-HARDWARE-EXECUTION v1 P3, H-2)
recreates `trg_execution_plan_v2_specs_insert_consistency` (migrations 9/17)
and `jobs_execution_pin_insert_guard` (migration 9) so an approval of kind
`hardware_action_v2` (payload contract `hardware-action-v2-approval-v1`) may
own an ExecutionPlan v2 spec and pin a Job exactly like `execution_plan_v2`.
Every other clause is unchanged; the step is idempotent (`DROP … IF EXISTS`).

## Operator commands

```text
dispatch db current --db path/to/jobqueue.db
dispatch db check --db path/to/jobqueue.db
dispatch db backup --db path/to/jobqueue.db --output backups/jobqueue.db
dispatch db upgrade --db path/to/jobqueue.db
dispatch db restore-verify --db backups/jobqueue.db
dispatch db audit-export --db path/to/jobqueue.db --output audit.jsonl
dispatch db audit-replay --db path/to/jobqueue.db \
  --operation-id OPERATION_ID --operator OPERATOR_ID --reason-code manual_replay
dispatch db audit-anchor --db path/to/jobqueue.db \
  --output /off-host/audit-checkpoint.json --database-id CONTROL_PLANE_ID \
  --signing-key-file /off-host/audit-signing.key
dispatch db restore-verify --db backups/jobqueue.db \
  --audit-anchor /off-host/audit-checkpoint.json \
  --signing-key-file /off-host/audit-signing.key
```

The deployment order is backup, `check`, `upgrade`, application rollout, and
smoke test. `current` and `check` do not instantiate the application. The
legacy `Database(path)` compatibility constructor still performs the same
idempotent versioned upgrade used by existing local/test callers; new
deployments should run the explicit CLI preflight before starting the service.

`backup` uses SQLite's online backup API and never mutates the source. A
restored copy must pass `PRAGMA integrity_check` and the durable audit
hash-chain check before it is considered a rollback input. Supplying
`--audit-anchor` to `restore-verify` additionally checks the independent
signature and the checkpoint's first/last sequence boundary against the
restored database (and, when supplied, the backup-manifest digest binding).
This local verification does not provision off-host immutable storage or
rotate signing keys; those remain deployment controls. There is no destructive
down-migration. Product Run rollback first disables
`RUN_EXPERIENCE_V2_ENABLED`; Project-bootstrap runtime rollback disables
`PROJECT_BOOTSTRAP_V2_ENABLED`; standalone Environment rollback independently
disables `PROJECT_ENVIRONMENTS_V1_ENABLED`; Dataset governance rollback disables
`DATASET_PUBLISH_V2_ENABLED`, then `DATASET_SHARING_V2_ENABLED`, and finally
`DATASET_ASSETS_V2_ENABLED`. Product RBAC rollback then disables
`PRODUCT_RBAC_V2_ENABLED` (and, when required, `API_V2_ENABLED`). AI-
engineering runtime rollback likewise disables flags only:
`EXPERIMENT_V2_ENABLED`, then `METRICS_V1_ENABLED`,
`AGENT_SESSION_V1_ENABLED`, `PROJECT_CONVERSATION_V1_ENABLED`, and
`CLAUDE_CODE_AGENT_V1`, retaining every conversation, session, metrics, and
experiment row. These steps retain additive tables, immutable resources,
approvals, idempotency rows, and audit evidence. A binary/schema rollback to
software that cannot read the current checked-in schema version requires
restoring a verified pre-upgrade backup and reinstalling the prior
application wheel.
