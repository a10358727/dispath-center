# DG-EXEC-ATTEMPT — Durable attempt / outbox decision gate

> Date: 2026-07-27
>
> Contract revision: `DG-EXEC-ATTEMPT-v1`
>
> Status: **approved on 2026-07-27 for WP-1A additive schema, narrow
> DB/domain APIs and pure tests; no production migration/cutover is authorized**
>
> Authoritative decision record: `docs/DECISIONS.md`
>
> Approved reviewed-draft SHA-256:
> `5ea026f855e7095e06e16cc124da809a0082b07ef2b493a2e28fd2813f15a40d`
>
> Required approval phrase:
> `DG-EXEC-ATTEMPT v1：核准本文件的 recommended contract`

This document is the exact review contract required between Phase 0 and WP-1A
in `docs/NEXT_IMPLEMENTATION_PLAN.md`. It does not enable a flag, create a
migration, touch a worker, or amend a canonical invariant.

## 1. Recommended decision

Approve an additive, default-off durable execution identity with these fixed
properties:

1. `jobs.status` and its canonical transitions do not change.
2. `execution_attempts` owns transport/execution identity; `liveness=unknown`
   is orthogonal to attempt state and never means failure.
3. `execution_operations` is a durable, authorization-bound outbox. A worker
   records intent and `effect_started_at` before an external side effect.
4. One partial unique index, not a Python read-then-write check, enforces at
   most one active generic attempt per Job.
5. Approved execution bytes, target revision, backend, authorization reference,
   digest and fencing identity are immutable after creation.
6. Existing rows remain honest legacy rows. No migration invents an approval,
   digest, server revision, attempt, or historical event.
7. Phase 1 writes shadow observations only. It does not route production work
   through the new attempt/outbox path.
8. SSH stays the production backend. Node assignment and SSH cutover remain
   blocked by their later named gates.

## 2. Existing behavior that must not change

- `INV-APPROVAL-1…5`, `INV-SSH-1…9`, `INV-NODE-1…6`,
  `INV-STATE-1…6`, `INV-AUDIT-1…2`, `INV-LLM-1…5` and
  `INV-TEST-1…2` remain canonical.
- `jobs.status` remains:
  `queued | running | blocked | done | failed | cancelled`.
- The existing SSH/SFTP/tmux/exit-code-sentinel command strings and ordering
  remain byte-for-byte unchanged during Phase 1.
- Unknown/unreachable state never produces `failed`, a new attempt, a new
  target, or backend fallback.
- `maybe_auto_approve()` remains exactly limited to `enqueue|stop`.
- Existing `node_attempts` remains a protocol-specific legacy extension. It is
  not silently reinterpreted as the new generic attempt table.
- No public HTTP, WebSocket, LLM or MCP mutation route is added by WP-1A/1B.
- No production flag is enabled and no production DB is migrated in this
  development task.

## 3. Exact state domains

### 3.1 Attempt state

```text
leased | dispatching | running
done | failed | expired | abandoned_before_launch
```

Active states are exactly `leased`, `dispatching`, and `running`.

Allowed transitions:

```text
leased      -> dispatching | expired
dispatching -> running | done | failed | abandoned_before_launch
running     -> done | failed
```

Rules:

- `leased` is Node pre-ack only. SSH starts at `dispatching`.
- `expired` is legal only when no acknowledgement and no external effect was
  started.
- `abandoned_before_launch` requires positive evidence that launch was never
  attempted and the workload cannot have started.
- `failed` means valid workload terminal evidence with non-zero exit status;
  it is not a transport error.
- Terminal attempt states never reopen. A retry creates a new attempt number.

### 3.2 Attempt liveness

```text
known | unknown
```

`known <-> unknown` may change while an attempt is active. It does not change
the Job status. `remote_unreachable`, heartbeat expiry, response loss and
credential emergency revocation set `unknown`; none of them creates a
terminal event or permits reassignment.

### 3.3 Operation state

```text
pending | processing | delivered | uncertain | failed
```

Allowed transitions:

```text
pending    -> processing | failed
processing -> pending | delivered | uncertain | failed
uncertain  -> delivered | failed
```

- A processing lease may return to `pending` only when
  `effect_started_at IS NULL`.
- Once `effect_started_at` is non-NULL, lease loss, crash, timeout or an
  ambiguous response becomes `uncertain`, never automatic retry.
- `failed` means validation failure or definite no-side-effect failure.
- `uncertain` requires reconciliation using the same attempt, operation,
  idempotency key, target and backend.
- `uncertain -> delivered|failed` requires authoritative read-only evidence.
  It never repeats the material operation. `uncertain -> failed` additionally
  requires positive evidence that the effect did not occur; absence, timeout
  or unreachable state is not such evidence.

### 3.4 Canonical Job projection

| Attempt state | Canonical Job state |
|---|---|
| `leased` before ack | `queued` |
| `dispatching` / `running`, whether liveness is known or unknown | `running` |
| `done` | `done` |
| `failed` | `failed` |
| `expired` before ack | `queued` |
| `abandoned_before_launch` | `queued` |

This gate does not authorize `running -> cancelled`. Approved stop remains an
intent; terminal state still requires sentinel/agent terminal evidence.

## 4. Exact additive schema contract

Names and domains below are fixed by this decision. WP-1A may add indexes or
non-semantic query helpers, but may not rename fields, widen state values, or
weaken checks without returning to a decision gate.

### 4.1 Existing table additions

`approvals` gains nullable:

```text
payload_sha256          TEXT
payload_contract_version TEXT
payload_immutable_at    TEXT
materialization_started_at TEXT
```

`jobs` gains nullable:

```text
execution_approval_id    INTEGER
approved_payload_sha256  TEXT
execution_contract_version TEXT
execution_contract_role  TEXT
approved_command_sha256  TEXT
```

`node_attempts` gains nullable:

```text
execution_attempt_id     TEXT
```

Migration rules:

- Every existing row receives `NULL`; no backfill is guessed.
- A non-NULL `approvals.payload_sha256` makes `payload`,
  `payload_sha256`, `payload_contract_version` and
  `payload_immutable_at` immutable through both a DB trigger and the service
  API.
- The three payload pin fields are set only by INSERT. A legacy row whose
  digest is NULL cannot later be "upgraded" by UPDATE and thereby bless
  possibly changed historical bytes.
- `materialization_started_at` is a one-way NULL -> timestamp CAS written with
  the authenticated decision actor before a multi-resource effect begins. It
  prevents a second approve/reject path from racing crash recovery without
  adding a new approval status value.
- Tests that intentionally tamper with legacy approvals remain possible
  because their digest is NULL. New pinned approvals fail closed on mutation.
- Pinned Job contract fields are also set only by INSERT and are immutable.
  For a pinned Job, `command`, `type`, `project`, `require_tag`, `pin_server`,
  `depends_on`, `gpus_needed` and `priority` are immutable; status, selected
  server and observation/result fields retain their narrow lifecycle updates.
- A Job is eligible for the generic attempt path only when all five execution
  contract fields are present, its role exists exactly once in the pinned
  contract, its command digest matches, and the approval is approved.
- A partial unique index on `node_attempts(execution_attempt_id)` where the
  value is non-NULL permits at most one protocol-specific Node row for a
  generic attempt. Linking requires the same `job_id`, a generic
  `backend=node` attempt and one service transaction; existing rows stay NULL.

### 4.2 `server_config_revisions`

```text
id                         TEXT PRIMARY KEY
server_name                TEXT NOT NULL
revision                   INTEGER NOT NULL
normalized_target_json     TEXT NOT NULL
credential_ref_json        TEXT NOT NULL
target_identity_sha256     TEXT NOT NULL
assignment_eligibility     TEXT NOT NULL
publication_state          TEXT NOT NULL
created_by_approval_id     INTEGER
created_at                 TEXT NOT NULL
activated_at               TEXT
retired_at                 TEXT
UNIQUE(server_name, revision)
CHECK assignment_eligibility IN ('approved', 'legacy_observed')
CHECK publication_state IN ('prepared', 'active', 'retired')
```

The immutable target contains only normalized execution backend, host, port,
user, `project_roots` and `dataset_roots`, plus an opaque credential-version
reference. The roots are the allowed work-root sets from the current
`ServerConfig`; an operation's exact working directory remains pinned in its
execution contract/payload and must be contained by an allowed root.

For the initial file provider, the reference is:

```text
{
  provider: "ssh-key-file-v1",
  version_id,
  real_path,
  file_identity: {device, inode, size, mtime_ns}
}
```

`version_id` and the path are fixed by the approved server revision.
`real_path` and `file_identity` are derived with `realpath/stat` only. The
control plane does not open or hash the private key while creating or
validating a revision, preserving the existing `app/server_config.py`
credential boundary. Private-key bytes are never copied to DB, payload, audit
or logs. Routine rotation uses a new versioned path/reference and retains the
old reference until active use is zero. A file-identity mismatch is
unknown/recovery-hold, not fallback.

`normalized_target_json`, `credential_ref_json`,
`target_identity_sha256`, `server_name`, `revision`,
`assignment_eligibility`, `created_by_approval_id` and `created_at` are
immutable. Publication changes use CAS:

```text
prepared -> active -> retired
prepared -----------> retired
```

Only `publication_state`, `activated_at` and `retired_at` may change, under
those transitions. `prepared -> retired` records an exact revision abandoned
or compensated before publication; it was never assignment-eligible. A
partial unique index enforces at most one `active` revision per server. New
assignment requires `assignment_eligibility=approved`,
`publication_state=active`, the referenced approval still `approved`, and no
unresolved server-config mutation journal.

Existing `servers.yaml` entries may be represented only as
`legacy_observed`; they are not eligible for a new generic attempt. A new
approved add/update (including an explicit no-op re-attestation) creates the
first `approved` revision. This avoids fabricating historical approval.
`assignment_eligibility=approved` requires a non-NULL
`created_by_approval_id`; the revision may remain `prepared` while that
approval is being materialized, but it cannot authorize assignment until the
approval row is `approved`. `legacy_observed` requires the reference to be
NULL.

Because SQLite and `servers.yaml` cannot commit atomically, revisions are
published through the mutation journal in the next section.

### 4.3 `server_config_mutations`

```text
id                         TEXT PRIMARY KEY
approval_id                INTEGER NOT NULL UNIQUE
operation                  TEXT NOT NULL
server_name                TEXT NOT NULL
prior_revision_id          TEXT
prepared_revision_id       TEXT
approved_payload_sha256    TEXT NOT NULL
yaml_before_sha256         TEXT NOT NULL
yaml_after_sha256          TEXT NOT NULL
state                      TEXT NOT NULL
created_at                 TEXT NOT NULL
yaml_applied_at            TEXT
activated_at               TEXT
last_error_category        TEXT
sanitized_error_detail     TEXT
CHECK operation IN ('add', 'update', 'disable', 'delete')
CHECK state IN ('intent', 'yaml_applied', 'activated', 'rolled_back',
                'recovery_hold')
```

`prepared_revision_id` is required for add/update and NULL for disable/delete.
A partial unique index allows only one
`intent|yaml_applied|recovery_hold` server-config mutation globally,
serializing atomic replacement of the single YAML document and preventing a
new mutation from bypassing unresolved recovery. Identity, approval, operation
and before/after digests are immutable.

The journal transition graph is closed:

```text
intent       -> yaml_applied | rolled_back | recovery_hold
yaml_applied -> activated | rolled_back | recovery_hold
recovery_hold -> yaml_applied | rolled_back
```

`activated` and `rolled_back` are terminal. Recovery may leave
`recovery_hold` only after current YAML positively matches the same journal's
exact after digest (resume) or before digest (roll back). Absence, parse
failure or any third digest remains on hold.

Approved server mutation uses this publication protocol:

1. In one DB transaction, verify the pinned pending approval, CAS
   `materialization_started_at`, record decision actor, insert an immutable
   mutation intent and, for add/update, its prepared revision.
2. Commit the DB intent before touching YAML.
3. Perform canonical backup → atomic YAML replace. Both legacy and generic
   schedulers treat this server as ineligible while its journal is unresolved.
4. CAS the journal to `yaml_applied` only if the exact expected YAML digest is
   now present.
5. Reload the in-memory configuration from the exact on-disk digest while the
   unresolved journal still blocks both schedulers.
6. In one final DB transaction, activate/retire matching revisions as
   required, CAS the journal to `activated` and mark the approval approved.
   Only this final state restores assignment eligibility.

Crash recovery compares current YAML with prepared/active digests:

- before YAML replace: the mutation intent and prepared revision remain
  ineligible; the same approval may resume its exact bytes, but no competing
  mutation may pass;
- after YAML replace but before activation: the journal can prove the exact
  `yaml_after_sha256`; new assignment to that server fails closed until
  activation;
- after in-memory reload but before finalization: recovery reloads the same
  verified YAML and then performs the single final DB transaction; schedulers
  still see the unresolved journal and cannot dispatch in this window;
- a mismatch is never auto-selected or rewritten. It remains
  `recovery_hold` until explicit operator recovery re-establishes this same
  journal's exact before or already-approved after bytes; a competing mutation
  approval cannot bypass the hold.

If YAML replace succeeds but validation/reload fails, automatic compensation
is allowed only when the same mutation's backup hashes to
`yaml_before_sha256` and the current file still hashes to
`yaml_after_sha256`. Compensation atomically restores and reloads those exact
prior bytes, leaves any unpublished prepared revision permanently ineligible,
records `rolled_back` and rejects the approval in one DB transaction. A
definite failure before replacement may use the same rolled-back terminal only
after proving current YAML still matches `yaml_before_sha256`. If either
digest differs, the journal enters `recovery_hold`; no automatic overwrite,
assignment or new server mutation is allowed.

Delete is represented by a mutation journal plus retirement of the prior
revision, not a fabricated "empty target" revision. Disabled/deleted targets
remain resolvable by active attempts through retained prior revisions.

### 4.4 `execution_attempts`

```text
id                         TEXT PRIMARY KEY
job_id                     INTEGER NOT NULL
attempt_number             INTEGER NOT NULL CHECK(attempt_number >= 1)
backend                    TEXT NOT NULL CHECK(backend IN ('ssh', 'node'))
server_name                TEXT NOT NULL
server_config_revision_id  TEXT NOT NULL
target_identity_sha256     TEXT NOT NULL
execution_approval_id      INTEGER NOT NULL
approved_payload_sha256    TEXT NOT NULL
execution_contract_version TEXT NOT NULL
state                      TEXT NOT NULL
liveness                   TEXT NOT NULL
fencing_token              TEXT NOT NULL UNIQUE
scheduler_fencing_epoch    INTEGER NOT NULL
recovery_hold_reason       TEXT
created_at                 TEXT NOT NULL
lease_expires_at           TEXT
dispatch_intent_at         TEXT
acknowledged_at            TEXT
last_observed_at           TEXT
stop_requested_at          TEXT
stop_acknowledged_at       TEXT
terminal_at                TEXT
exit_code                  INTEGER
last_error_category        TEXT
sanitized_error_detail     TEXT
UNIQUE(job_id, attempt_number)
```

The DB must create:

```sql
CREATE UNIQUE INDEX ... ON execution_attempts(job_id)
WHERE state IN ('leased', 'dispatching', 'running');
```

Fixed identity fields from `job_id` through `scheduler_fencing_epoch` plus
`created_at` are immutable. State/liveness changes use compare-and-swap and
append an event in the same transaction.

Generic attempt rows do not allow NULL legacy authorization. Legacy execution
continues through the legacy scheduler until drained or explicitly re-created
under a pinned contract; no unsafe "legacy exception" is added to the new
outbox.

Database checks close the `backend`, `state`, `liveness` and
`recovery_hold_reason` domains. A non-NULL hold is exactly
`security_credential_revoked` or `manual_recovery_review`. Terminal timestamp
and exit-code consistency is enforced by narrow transition methods plus
transition tests; transport/unreachable errors can never populate a workload
terminal state.

### 4.5 `execution_operations`

```text
id                           TEXT PRIMARY KEY
attempt_id                   TEXT NOT NULL
operation                    TEXT NOT NULL
idempotency_key              TEXT NOT NULL UNIQUE
authorization_approval_id    INTEGER
authorization_class          TEXT NOT NULL
authorized_contract_sha256   TEXT
payload_json                 TEXT NOT NULL
payload_sha256               TEXT NOT NULL
state                        TEXT NOT NULL
claim_owner                  TEXT
claim_fencing_epoch          INTEGER
claim_expires_at             TEXT
effect_started_at            TEXT
attempt_count                INTEGER NOT NULL DEFAULT 0
retry_at                     TEXT
created_at                   TEXT NOT NULL
updated_at                   TEXT NOT NULL
last_error_category          TEXT
sanitized_error_detail       TEXT
```

Operation and authorization mapping is closed:

| operation | authorization class | actual approval requirement |
|---|---|---|
| `prepare` | `execution` | approved pinned execution contract |
| `launch` | `execution` | approved pinned execution contract |
| `inspect` | `inspect` | no mutation approval; fixed read-only command set |
| `stop` | `stop` | approved existing `kind=stop`, bound to attempt/job/digest |
| `collect` | `execution` | approved contract explicitly includes collection scope |
| `cleanup` | `cleanup` | approved new `kind=attempt_cleanup`, exact paths/retention |

For every operation except `inspect`,
`authorization_approval_id` and `authorized_contract_sha256` are non-NULL.
For `inspect` both are NULL. `stop` can never reuse an execution approval, and
`cleanup` can never be inferred from terminal state.

Identity, operation, authorization, payload and digest fields are immutable.
Claim/retry/result fields are mutable only through narrow CAS methods.

`attempt_cleanup` is approved as a future material approval kind by this gate,
but WP-1A does not add a public request route or execute cleanup. It never joins
the auto-approve allowlist.

`collect` outcome is independent of workload terminal state. A done/failed
attempt and Job remain terminal if result collection later fails; collection
gets its own operation status/reason and retry policy.

### 4.6 `execution_attempt_events`

```text
id                         INTEGER PRIMARY KEY AUTOINCREMENT
attempt_id                 TEXT NOT NULL
operation_id               TEXT
event_type                 TEXT NOT NULL
from_state                 TEXT
to_state                   TEXT
from_liveness              TEXT
to_liveness                TEXT
reason_code                TEXT NOT NULL
evidence_json              TEXT NOT NULL
event_sha256               TEXT NOT NULL
created_at                 TEXT NOT NULL
```

This table is append-only. DB triggers reject UPDATE and DELETE. Evidence is
sanitized metadata/digests, never command, credential, token or private-key
bytes. It complements, but does not replace, `audit.jsonl`.

### 4.7 `execution_shadow_observations`

```text
id                         INTEGER PRIMARY KEY AUTOINCREMENT
tick_id                    TEXT NOT NULL
job_id                     INTEGER NOT NULL
proposed_backend           TEXT
proposed_server_name       TEXT
proposed_revision_id       TEXT
approved_payload_sha256    TEXT
legacy_status_before       TEXT NOT NULL
legacy_status_after        TEXT
parity_result              TEXT NOT NULL
reason_code                TEXT NOT NULL
created_at                 TEXT NOT NULL
```

Shadow observations never create an attempt/operation, claim ownership, write a
Job, or call SSH/Node. They are measurements only.

### 4.8 `legacy_job_stop_intents`

This table covers the WP-1C stop correction for running legacy SSH Jobs which
honestly have no generic attempt:

```text
id                         TEXT PRIMARY KEY
job_id                     INTEGER NOT NULL
stop_approval_id           INTEGER NOT NULL
approved_payload_sha256    TEXT NOT NULL
backend                    TEXT NOT NULL
server_name                TEXT NOT NULL
state                      TEXT NOT NULL
requested_at               TEXT NOT NULL
delivery_started_at        TEXT
delivered_at               TEXT
terminal_observed_at       TEXT
last_error_category        TEXT
sanitized_error_detail     TEXT
CHECK state IN ('requested', 'delivery_uncertain', 'delivered',
                'terminal_observed')
CHECK backend = 'ssh'
```

- This table is used only when the Job has no generic attempt. A generic
  attempt uses its single `execution_operations(operation=stop)` row instead;
  the two stores are never dual-written as competing stop truth.
- New stop approvals are pinned on INSERT. Existing pending legacy stop
  approvals are not retrofitted; the operator must reject/re-create them before
  the corrected path is used.
- One partial unique index permits at most one unresolved stop intent per Job.
- The closed transition graph is:

  ```text
  requested          -> delivery_uncertain | delivered | terminal_observed
  delivery_uncertain -> delivered | terminal_observed
  delivered          -> terminal_observed
  ```

- An unresolved stop intent prevents the legacy reconciler's
  `tmux gone + no sentinel -> queued` branch from relaunching the workload.
- Kill success means `delivered`, not terminal. Kill failure/unreachable means
  `delivery_uncertain`; the Job remains running in both cases.
- Only matching sentinel/agent terminal evidence moves the intent to
  `terminal_observed` and projects Job done/failed.

This seam does not authorize `running -> cancelled` and does not fabricate a
generic attempt for an already-running legacy Job.

### 4.9 `scheduler_leases`

```text
name                       TEXT PRIMARY KEY
owner_id                   TEXT NOT NULL
fencing_epoch              INTEGER NOT NULL
lease_expires_at           TEXT NOT NULL
updated_at                 TEXT NOT NULL
```

The initial singleton name is `execution-attempt-v1`. Acquisition/renewal uses
`BEGIN IMMEDIATE` plus CAS. A new owner increments `fencing_epoch`; stale
owners cannot create attempts, claim operations or commit transitions.
Lease comparisons use SQLite connection time, not independently sampled worker
wall clocks.
`execution_attempts.scheduler_fencing_epoch` remains immutable evidence of the
epoch that created the attempt. Each later mutation validates the current live
lease separately, so a newer fenced leader may reconcile older active
attempts; it does not rewrite their creation epoch. An operation claim records
the current epoch in `claim_fencing_epoch`.

The new tables declare non-cascading `REFERENCES ... ON DELETE RESTRICT` for:

```text
server_config_revisions.created_by_approval_id -> approvals.id
server_config_mutations.approval_id -> approvals.id
server_config_mutations.prior_revision_id -> server_config_revisions.id
server_config_mutations.prepared_revision_id -> server_config_revisions.id
execution_attempts.job_id -> jobs.id
execution_attempts.server_config_revision_id -> server_config_revisions.id
execution_attempts.execution_approval_id -> approvals.id
execution_operations.attempt_id -> execution_attempts.id
execution_operations.authorization_approval_id -> approvals.id
execution_attempt_events.attempt_id -> execution_attempts.id
execution_attempt_events.operation_id -> execution_operations.id
legacy_job_stop_intents.job_id -> jobs.id
legacy_job_stop_intents.stop_approval_id -> approvals.id
```

WP-1A enables `PRAGMA foreign_keys=ON` on every new `Database` connection
before schema initialization and verifies it. This repository currently
declares no legacy foreign keys, so this does not reinterpret or validate
historical relationships. The nullable columns added to existing tables
cannot gain a SQLite foreign key through the repository's additive
`ALTER TABLE ADD COLUMN` migration; their linkage is therefore checked by
narrow service methods and insert/update triggers in the same transaction.
No foreign-key cascade is introduced.

Database `CHECK` constraints close attempt backend/state/liveness/hold and
operation name/state/authorization-class domains. Compound operation checks
require `inspect` to have NULL authorization fields and every material
operation to have non-NULL authorization fields. Approval-kind/digest/target
matching remains a transactional service revalidation because SQLite `CHECK`
constraints cannot query another table.

DB triggers reject DELETE of server revisions, server-config mutations,
attempts, operations, attempt events, shadow observations and legacy stop
intents, and reject UPDATE of every immutable column. Attempt events and
shadow observations also reject every UPDATE. Mutable lifecycle columns remain
writable only through narrow CAS methods.

## 5. Immutable execution contract

Only new, explicitly versioned execution-bearing approvals are pinned. Initial
accepted versions are:

```text
enqueue-execution-v1
auto-placement-execution-v1
dataset-prewarm-execution-v1
```

Other pinned material contracts introduced by the same foundation are:

```text
server-config-v1
stop-intent-v1
attempt-cleanup-v1
```

These names do not create auto-approval authority. They only define immutable
payload shapes for their existing/new approval kinds.

Coding/engineering jobs and unknown legacy kinds remain ineligible until a
later exact contract is approved; they continue on the legacy SSH path.

Canonical JSON rules:

- UTF-8, dictionary keys sorted, separators `,` and `:`, no insignificant
  whitespace, `ensure_ascii=false`.
- Allowed values: object, array, string, integer, boolean and null. Floats,
  NaN/Infinity, duplicate keys and non-string keys are rejected.
- Command bytes are represented as `command_utf8_b64` plus `command_sha256`.
- The contract includes authorized operations, project/version/data/profile
  references when present and resource/placement constraints.
- The database-generated approval ID is not embedded in its own canonical
  payload, which would create a circular insert/hash dependency. Every
  downstream Job, attempt, operation and event instead binds the immutable
  tuple `(approval_id, payload_sha256, payload_contract_version)`.
- The contract contains `job_specs`, each with a unique stable role,
  `command_utf8_b64`, `command_sha256`, type, placement/resource constraints
  and dependency roles. Generated DB Job IDs are not part of the approval.
  Each created Job stores its role and approved command digest, which is how a
  multi-Job setup/sync/main plan maps back to exact approved bytes.
- Content changes create a new approval. Approve-time revalidation accepts or
  rejects; it never rewrites the payload.

Attempt target/revision is chosen after approval only within the approved
placement constraints. The attempt then fixes that choice immutably.

## 6. Transaction and concurrency rules

All material methods use one SQLite transaction with `BEGIN IMMEDIATE`:

1. Re-read Job, approval, contract digest, target revision and leader epoch.
2. Verify eligibility and absence of an active generic/legacy owner.
3. Insert attempt/operation intent and matching event.
4. Project the canonical Job transition when applicable.
5. Commit.
6. Only after commit may a worker contact SSH/Node or write remote bytes.

Every state/liveness update is a CAS (`WHERE state=? ...`). Zero affected rows
means conflict; it is never retried as an unconditional update.

Approval-to-Job publication for a pinned execution contract is also one DB
transaction: revalidate the pending approval, insert every contract role Job
with its immutable linkage, update approval status, and append DB-side
contract evidence. Audit remains best-effort append after commit as required by
the existing audit invariant. Partial Job chains are never published.

The outbox worker:

1. Claims a pending row under a current fencing epoch.
2. Revalidates approval status, actual kind, attempt/job/target and all digests.
3. Persists `effect_started_at` before the first external-effect call.
4. Executes only the fixed operation payload.
5. Writes delivered/uncertain/failed plus an event.

An `uncertain` material operation is never claimed for another effect call.
A reconciler may use only the fixed `inspect` command set and matching durable
receipt/sentinel/agent evidence to finalize it as delivered or as a definite
no-effect failure.

The initial reason-code vocabulary is closed:

```text
approval_missing
approval_not_approved
approval_kind_mismatch
contract_digest_mismatch
target_revision_missing
target_identity_mismatch
leader_lease_lost
claim_conflict
contract_validated
remote_state_observed
pre_effect_definite_failure
effect_outcome_unknown
remote_unreachable
terminal_evidence_valid
security_credential_revoked
manual_recovery_hold
```

Adding a semantic reason code requires a test and documentation update.
`contract_validated` is the success reason for the same-transaction
attempt/operation creation event required above; it never means that a remote
effect started or completed.
`remote_state_observed` means matching positive acknowledgement, receipt,
sentinel or agent evidence was validated; it is not inferred from reachability
loss or lease expiry.

## 7. Server mutation and credential rules

- Disable prevents new assignment; active attempts retain and reconcile through
  their immutable revision.
- Delete, host/user/port/root-set repoint, backend change and routine
  credential retirement fail closed while referenced by:
  - an active generic attempt;
  - a pending/processing/uncertain operation;
  - an unlinked, unexpired `status=leased` legacy `node_attempts` row;
  - an unlinked `status=acked|running` legacy `node_attempts` row, regardless
    of heartbeat freshness;
  - an enrolled non-revoked Node binding.
- Routine key rotation creates a new revision/reference and keeps the old
  version resolvable until active/reference counts reach zero.
- Emergency credential revocation may immediately revoke one credential.
  Affected attempts become `liveness=unknown` with
  `recovery_hold_reason=security_credential_revoked`; they are not failed,
  retargeted or relaunched through SSH.
- Legacy scheduler eligibility permanently checks generic active attempts and
  unlinked active Node rows, including after rollback.
- Both schedulers reject new assignment to a server with an unresolved
  `server_config_mutations` journal, including the YAML/reload crash windows.
- A prepared-but-not-active revision never authorizes assignment. Server-config
  recovery uses its publication digest and approval reference; it never treats
  "currently present in YAML" alone as proof of approval.

## 8. Feature flags and rollout

Flags are intentionally separate:

```text
EXECUTION_ATTEMPT_SHADOW_ENABLED=false
EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=false
EXECUTION_ATTEMPT_RECONCILE_EXISTING=false
EXECUTION_OUTBOX_WORKER_ENABLED=false
```

- Phase 1 may enable shadow only in a controlled non-production environment.
- `NEW_CLAIMS` remains false until the Phase 2 gates.
- Enabling `NEW_CLAIMS` requires both `RECONCILE_EXISTING` and
  `OUTBOX_WORKER` to be true; an invalid combination fails at configuration
  validation before background loops start.
- Once any non-terminal generic attempt or operation exists, startup with
  `RECONCILE_EXISTING=false` fails closed and reports unhealthy; it does not
  silently abandon ownership.
- Rollback sets `NEW_CLAIMS=false`; it does not disable
  `RECONCILE_EXISTING`, remove ownership guards or delete schema/evidence.
- There is no umbrella flag whose off state abandons active work.

## 9. Phase 1 API and UI diff

WP-1A/1B adds **no public mutation API, no LLM/MCP tool and no UI action**.
Schema/domain methods and shadow measurements are internal.

Any future read endpoint or operator recovery action must be separately added
to the authorization catalog and tests. Manual recovery cannot mean "create a
second attempt"; `DG-ATTEMPT-RECOVERY` remains required.

## 10. Invariant compatibility and deferred decisions

No canonical invariant text changes under this gate:

- `INV-STATE-1`: new durable truth remains in `jobqueue.db`.
- `INV-STATE-2`: DB intent still precedes remote effects. Phase 1 does not
  activate the ambiguous launch path.
- `INV-STATE-3`: all schema changes are additive and migration-tested.
- `INV-STATE-4`: Job states/transitions remain closed.
- `INV-SSH-*`: SSH dependencies/commands/sentinel semantics do not change.
- `INV-NODE-*`: generic ownership strengthens, but does not activate, Node.
- `INV-APPROVAL-*`: every material operation has an exact approval contract;
  auto-approve remains unchanged.

Still deferred:

- `DG-AMBIGUOUS-LAUNCH`: exact `INV-STATE-2` revision, remote atomic claim,
  response-loss replay and receipts.
- `DG-JOB-STATE`: any new Job state or `running -> cancelled`.
- `DG-ATTEMPT-RECOVERY`: abandoning acknowledged/unknown work.
- `DG-NODE-V2`: server-selected lease and runnable agent.
- `DG-DATASET-SNAPSHOT` / `DG-CODE-PROMOTE`: immutable data/code publish.

## 11. WP-1A/1B acceptance after approval

- Fresh and representative legacy DB migrations pass; every old value remains
  NULL/legacy rather than fabricated.
- Two Database connections racing to create an attempt produce one winner.
- Partial unique index prevents two active attempts even if service checks are
  bypassed.
- Pinned approval payload, attempt identity, server revision and operation
  authorization/payload cannot be mutated through SQL or service APIs.
- A multi-Job approved contract maps setup/sync/main roles to distinct command
  digests; role swapping or mutable `jobs.command` tampering fails closed.
- Crash tests at each server revision publication boundary converge to old
  active, new active, or explicit fail-closed prepared state; never two active
  revisions or an unapproved assignment.
- Reload failure compensates only with exact before/after digests; digest drift
  enters `recovery_hold` and blocks assignment/new mutation.
- Missing/wrong/stale approval, digest, target or fencing epoch yields zero
  remote calls.
- Processing lease expiry before `effect_started_at` can reclaim; after it,
  operation becomes uncertain and cannot auto-retry.
- Server delete/repoint/rotation guards cover generic, legacy Node and enrolled
  Node ownership.
- Legacy stop kill failure/unreachable persists an unresolved stop intent,
  leaves Job running and blocks automatic requeue; only terminal evidence
  closes it.
- Shadow mode performs zero remote calls and zero canonical writes.
- Legacy SSH exact-string golden tests, migration/core/static/full suite pass.
- Rollback drill stops new claims while preserving reconciliation ownership.

## 12. Decision

- [x] **Approve recommended contract** and authorize WP-1A additive schema,
  narrow DB/domain APIs and pure transition/concurrency tests. All flags remain
  at their defaults; no production migration/cutover is authorized.
- [ ] Approve with changes: ______________________________________________
- [ ] Reject; keep the current legacy SSH scheduler and do not start WP-1A.

The first box was explicitly approved on 2026-07-27 and is recorded in
`docs/DECISIONS.md`. This unblocks only the WP-1A scope written above; later
activation, cutover and invariant-changing gates remain blocked.

---

# Addendum v1.1 — RB-SERVER-001 pinned server-config publication

> Date: 2026-07-27
>
> Contract revision: `DG-EXEC-ATTEMPT-v1.1`
>
> Status: **approved on 2026-07-27 for the `RB-SERVER-001` publication rework
> and the operator recovery surface. `NEW_CLAIMS` stays `false`.** Sections
> 1–12 above are the approved `DG-EXEC-ATTEMPT-v1` text and are **unmodified**
> by this addendum.
>
> Digest of this file immediately before the addendum was appended:
> `09aa99ebcc80e5b7884121aae3d4c290a0f9259cc8b605d66976c69f6a1e88dd`
>
> Approved reviewed-draft SHA-256 of this file including the addendum:
> `3a8eb432962796bfa10e25ccc6acada19a07f42a14233248409ee48f6d9d1807`
>
> Required approval phrase:
> `DG-EXEC-ATTEMPT v1.1：核准 RB-SERVER-001 pinned publication addendum`

## A1. Why a separate ruling is needed

`DG-EXEC-ATTEMPT-v1` §4.2/§4.3 already approved the `server_config_revisions`
and `server_config_mutations` schema and the publication protocol in §7. WP-1B
wired the dual-read ownership guards for delete/repoint/rotation.

The remaining gap recorded as `RB-SERVER-001` is narrower and is **not** a
schema question:

> Existing public server approvals still write legacy YAML directly and do not
> materialize a pinned immutable revision/journal, so those targets stay
> `assignment_eligibility='legacy_observed'` and are ineligible for generic
> claims.

Closing it means changing the execution side of an existing, already-approved
public mutation path. §12 of the approved v1 text explicitly withheld that:
"No public HTTP, WebSocket, LLM or MCP mutation route is added by WP-1A/1B",
and the recorded ruling in `docs/DECISIONS.md` excludes "新 public/LLM/MCP
mutation route". This addendum asks only for that specific authorization.

## A2. Recommended scope

1. **No new approval kind and no new route.** The existing server
   add/update/disable/delete approval kinds and their existing endpoints stay
   exactly as they are, including `VALID_APPROVAL_KINDS` and the auto-approve
   allowlist.
2. **Approval execution is rerouted, not widened.** On approval, the handler
   runs the §7 publication protocol — `intent → yaml_applied → activated` with
   before/after YAML digests — instead of writing YAML and returning. The
   YAML file remains the operator-visible source and keeps its current format.
3. **New revisions from approved mutations are `assignment_eligibility=
   'approved'`.** Targets that were only observed from legacy YAML stay
   `legacy_observed` and stay ineligible for generic claims. No migration
   promotes a legacy target; re-approval is the only path.
4. **One operator recovery surface is added**, authenticated and in the
   authorization catalog: read the current mutation journal, and resolve a
   `recovery_hold` row by explicitly re-approving or rolling back. It cannot
   fabricate a revision, edit a digest, or activate two revisions.
5. **`NEW_CLAIMS` stays `false`** throughout this addendum's work, and the
   configuration validator additionally refuses `NEW_CLAIMS=true` while any
   `server_config_mutations` row is in `recovery_hold`.

## A3. Boundaries that do not move

- Approved payload bytes and digests stay immutable; compensation uses exact
  before/after digests and enters `recovery_hold` on drift (v1 §7).
- At most one `publication_state='active'` revision per server, enforced by the
  existing index rather than a Python check.
- Active attempts keep resolving their pinned `server_config_revision_id`;
  disable/delete still only blocks new assignment.
- Crash at any publication boundary converges to old-active, new-active, or an
  explicit fail-closed `prepared`/`recovery_hold` — never two active revisions
  and never an unapproved assignment target.
- LLM/MCP may still only create pending requests; the recovery surface is
  operator-only and is not exposed as an agent tool.

## A4. Acceptance

- A fresh approved add/update produces exactly one `active` revision, one
  journal row, and YAML whose digest matches `yaml_after_sha256`.
- Crash injected at each of `intent`, `yaml_applied`, `activated` converges as
  in A3; a digest mismatch on reload enters `recovery_hold` and blocks both new
  assignment and further mutation of that server.
- A `legacy_observed` target is rejected for generic claims with an explicit
  reason code, and is promoted only by a new approval.
- The recovery endpoint appears in the authorization catalog tests, rejects
  unauthenticated and LLM/MCP callers, and performs zero remote calls.
- Existing server-config API tests, approval tests, migration suite and full
  suite stay green; no production `servers.yaml` is touched.

## A5. Decision

- [x] **Approve addendum** — authorize rerouting approved server mutations
  through the pinned publication protocol plus the single operator recovery
  surface, with `NEW_CLAIMS` still `false`.
- [ ] Approve with changes: ______________________________________________
- [ ] Reject — leave `RB-SERVER-001` open; generic new claims then remain
  permanently blocked, since no target can ever become `approved`.

Approving this addendum does not enable `NEW_CLAIMS`, does not authorize the
SSH launch path (`DG-AMBIGUOUS-LAUNCH`), and does not change any canonical
invariant.
