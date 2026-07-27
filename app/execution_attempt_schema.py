"""Additive DG-EXEC-ATTEMPT-v1 database foundation.

This module contains only schema and database-enforced invariants.  Importing
it has no runtime effect; :class:`app.db.Database` installs the schema while
initialising its local SQLite connection.  No scheduler or remote backend
reads these tables in WP-1A.
"""

EXECUTION_ATTEMPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS server_config_revisions (
    id TEXT PRIMARY KEY,
    server_name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    normalized_target_json TEXT NOT NULL,
    credential_ref_json TEXT NOT NULL,
    target_identity_sha256 TEXT NOT NULL,
    assignment_eligibility TEXT NOT NULL
        CHECK (assignment_eligibility IN ('approved', 'legacy_observed')),
    publication_state TEXT NOT NULL
        CHECK (publication_state IN ('prepared', 'active', 'retired')),
    created_by_approval_id INTEGER
        REFERENCES approvals(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    retired_at TEXT,
    -- WP-2B (gate D-5): the atomic `mkdir` claim needs a local filesystem.
    -- A target whose agent_jobs path is on NFS/CIFS/FUSE fails closed to the
    -- legacy SSH path rather than silently losing claim atomicity.
    attempt_backend_preflight TEXT
        CHECK (
            attempt_backend_preflight IS NULL
            OR attempt_backend_preflight IN (
                'eligible', 'ineligible_non_local_fs', 'unknown'
            )
        ),
    UNIQUE (server_name, revision),
    CHECK (
        (assignment_eligibility = 'approved'
         AND created_by_approval_id IS NOT NULL)
        OR
        (assignment_eligibility = 'legacy_observed'
         AND created_by_approval_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS server_config_mutations (
    id TEXT PRIMARY KEY,
    approval_id INTEGER NOT NULL UNIQUE
        REFERENCES approvals(id) ON DELETE RESTRICT,
    operation TEXT NOT NULL
        CHECK (operation IN ('add', 'update', 'disable', 'delete')),
    server_name TEXT NOT NULL,
    prior_revision_id TEXT
        REFERENCES server_config_revisions(id) ON DELETE RESTRICT,
    prepared_revision_id TEXT
        REFERENCES server_config_revisions(id) ON DELETE RESTRICT,
    approved_payload_sha256 TEXT NOT NULL,
    yaml_before_sha256 TEXT NOT NULL,
    yaml_after_sha256 TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (
            state IN (
                'intent', 'yaml_applied', 'activated', 'rolled_back',
                'recovery_hold'
            )
        ),
    created_at TEXT NOT NULL,
    yaml_applied_at TEXT,
    activated_at TEXT,
    last_error_category TEXT,
    sanitized_error_detail TEXT,
    CHECK (
        (operation IN ('add', 'update') AND prepared_revision_id IS NOT NULL)
        OR
        (operation IN ('disable', 'delete') AND prepared_revision_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS execution_attempts (
    id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL
        REFERENCES jobs(id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    backend TEXT NOT NULL CHECK (backend IN ('ssh', 'node')),
    server_name TEXT NOT NULL,
    server_config_revision_id TEXT NOT NULL
        REFERENCES server_config_revisions(id) ON DELETE RESTRICT,
    target_identity_sha256 TEXT NOT NULL,
    execution_approval_id INTEGER NOT NULL
        REFERENCES approvals(id) ON DELETE RESTRICT,
    approved_payload_sha256 TEXT NOT NULL,
    execution_contract_version TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (
            state IN (
                'leased', 'dispatching', 'running', 'done', 'failed',
                'expired', 'abandoned_before_launch'
            )
        ),
    liveness TEXT NOT NULL CHECK (liveness IN ('known', 'unknown')),
    fencing_token TEXT NOT NULL UNIQUE,
    scheduler_fencing_epoch INTEGER NOT NULL CHECK (scheduler_fencing_epoch >= 1),
    recovery_hold_reason TEXT
        CHECK (
            recovery_hold_reason IS NULL
            OR recovery_hold_reason IN (
                'security_credential_revoked', 'manual_recovery_review'
            )
        ),
    created_at TEXT NOT NULL,
    lease_expires_at TEXT,
    dispatch_intent_at TEXT,
    acknowledged_at TEXT,
    last_observed_at TEXT,
    stop_requested_at TEXT,
    stop_acknowledged_at TEXT,
    terminal_at TEXT,
    exit_code INTEGER,
    last_error_category TEXT,
    sanitized_error_detail TEXT,
    -- WP-2B (DG-AMBIGUOUS-LAUNCH-v1 §7). NULL on every legacy row: an attempt
    -- created before this contract has no claim, no receipt and no recorded
    -- verdict, and inventing one would fabricate history.
    remote_claim_state TEXT
        CHECK (
            remote_claim_state IS NULL
            OR remote_claim_state IN (
                'unclaimed', 'launcher_claimed', 'controller_abandoned',
                'claim_unknown'
            )
        ),
    launch_receipt_sha256 TEXT,
    remote_boot_id TEXT,
    launcher_contract_version TEXT,
    prelaunch_verdict TEXT
        CHECK (
            prelaunch_verdict IS NULL
            OR prelaunch_verdict IN ('definite_not_launched', 'ambiguous')
        ),
    UNIQUE (job_id, attempt_number)
);

CREATE TABLE IF NOT EXISTS execution_operations (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL
        REFERENCES execution_attempts(id) ON DELETE RESTRICT,
    operation TEXT NOT NULL
        CHECK (
            operation IN (
                'prepare', 'launch', 'inspect', 'stop', 'collect', 'cleanup'
            )
        ),
    idempotency_key TEXT NOT NULL UNIQUE,
    authorization_approval_id INTEGER
        REFERENCES approvals(id) ON DELETE RESTRICT,
    authorization_class TEXT NOT NULL
        CHECK (
            authorization_class IN ('execution', 'inspect', 'stop', 'cleanup')
        ),
    authorized_contract_sha256 TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (
            state IN ('pending', 'processing', 'delivered', 'uncertain', 'failed')
        ),
    claim_owner TEXT,
    claim_fencing_epoch INTEGER,
    claim_expires_at TEXT,
    effect_started_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error_category TEXT,
    sanitized_error_detail TEXT,
    -- WP-2B: the persisted evidence behind a definite/ambiguous verdict.
    -- 'not_transmitted' is only ever written together with a definite
    -- pre-launch reason code.
    transmission_state TEXT
        CHECK (
            transmission_state IS NULL
            OR transmission_state IN ('not_transmitted', 'transmitted', 'unknown')
        ),
    CHECK (
        (operation IN ('prepare', 'launch', 'collect')
         AND authorization_class = 'execution')
        OR (operation = 'inspect' AND authorization_class = 'inspect')
        OR (operation = 'stop' AND authorization_class = 'stop')
        OR (operation = 'cleanup' AND authorization_class = 'cleanup')
    ),
    CHECK (
        (operation = 'inspect'
         AND authorization_approval_id IS NULL
         AND authorized_contract_sha256 IS NULL)
        OR
        (operation <> 'inspect'
         AND authorization_approval_id IS NOT NULL
         AND authorized_contract_sha256 IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS execution_attempt_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL
        REFERENCES execution_attempts(id) ON DELETE RESTRICT,
    operation_id TEXT
        REFERENCES execution_operations(id) ON DELETE RESTRICT,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    from_liveness TEXT,
    to_liveness TEXT,
    reason_code TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    event_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_shadow_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    proposed_backend TEXT,
    proposed_server_name TEXT,
    proposed_revision_id TEXT,
    approved_payload_sha256 TEXT,
    legacy_status_before TEXT NOT NULL,
    legacy_status_after TEXT,
    parity_result TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legacy_job_stop_intents (
    id TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL
        REFERENCES jobs(id) ON DELETE RESTRICT,
    stop_approval_id INTEGER NOT NULL
        REFERENCES approvals(id) ON DELETE RESTRICT,
    approved_payload_sha256 TEXT NOT NULL,
    backend TEXT NOT NULL CHECK (backend = 'ssh'),
    server_name TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (
            state IN (
                'requested', 'delivery_uncertain', 'delivered',
                'terminal_observed'
            )
        ),
    requested_at TEXT NOT NULL,
    delivery_started_at TEXT,
    delivered_at TEXT,
    terminal_observed_at TEXT,
    last_error_category TEXT,
    sanitized_error_detail TEXT
);

-- WP-3A (DG-DATASET-SNAPSHOT-v1 §7). A snapshot is immutable after publish:
-- its descriptor, manifest digest and shard mapping are what a run points at
-- to state which bytes it consumed.
CREATE TABLE IF NOT EXISTS dataset_snapshots (
    id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    dataset_version TEXT,
    state TEXT NOT NULL
        CHECK (state IN ('candidate', 'building', 'published',
                         'aborted', 'verification_unknown')),
    source_candidate_digest TEXT NOT NULL,
    manifest_digest TEXT,
    manifest_path TEXT,
    descriptor_path TEXT,
    store_revision TEXT NOT NULL,
    shard_policy_json TEXT NOT NULL,
    file_count INTEGER,
    total_bytes INTEGER,
    build_approval_id INTEGER
        REFERENCES approvals(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    last_error_category TEXT,
    sanitized_error_detail TEXT,
    CHECK (
        state <> 'published'
        OR (manifest_digest IS NOT NULL
            AND descriptor_path IS NOT NULL
            AND build_approval_id IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS dataset_snapshot_shards (
    snapshot_id TEXT NOT NULL
        REFERENCES dataset_snapshots(id) ON DELETE RESTRICT,
    shard_index INTEGER NOT NULL CHECK (shard_index >= 0),
    shard_sha256 TEXT NOT NULL,
    shard_bytes INTEGER NOT NULL CHECK (shard_bytes >= 0),
    file_count INTEGER NOT NULL CHECK (file_count >= 0),
    PRIMARY KEY (snapshot_id, shard_index)
);

-- WP-3B (plan §8.4). An ExecutionPlan binds immutable revision identifiers
-- only, never a mutable head: binding a head would let a run change meaning
-- between preview, approval and execution with nothing recording that it had.
CREATE TABLE IF NOT EXISTS execution_plans (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    contract_version TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    command_sha256 TEXT NOT NULL,
    reproducible INTEGER NOT NULL CHECK (reproducible IN (0, 1)),
    project_version_id TEXT
        REFERENCES project_versions(id) ON DELETE RESTRICT,
    run_profile_id TEXT
        REFERENCES run_profiles(id) ON DELETE RESTRICT,
    dataset_snapshot_id TEXT
        REFERENCES dataset_snapshots(id) ON DELETE RESTRICT,
    dataset_none INTEGER NOT NULL DEFAULT 0 CHECK (dataset_none IN (0, 1)),
    server_config_revision_id TEXT NOT NULL
        REFERENCES server_config_revisions(id) ON DELETE RESTRICT,
    request_approval_id INTEGER
        REFERENCES approvals(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    -- A reproducible plan must pin code, profile and data.  `dataset_none` is
    -- a pinned statement ("no data"); an unset dataset is not.
    CHECK (
        reproducible = 0
        OR (project_version_id IS NOT NULL
            AND run_profile_id IS NOT NULL
            AND (dataset_snapshot_id IS NOT NULL OR dataset_none = 1))
    ),
    CHECK (dataset_none = 0 OR dataset_snapshot_id IS NULL)
);

CREATE TABLE IF NOT EXISTS scheduler_leases (
    name TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    fencing_epoch INTEGER NOT NULL CHECK (fencing_epoch >= 1),
    lease_expires_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


# Installed only after legacy columns have been added.  Triggers in this block
# refer to those columns and would otherwise make an old DB fail before its
# additive ALTER TABLE migration can run.
EXECUTION_ATTEMPT_POST_MIGRATION_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_server_config_one_active_revision
    ON server_config_revisions(server_name)
    WHERE publication_state = 'active';
CREATE UNIQUE INDEX IF NOT EXISTS idx_server_config_one_unresolved_mutation
    ON server_config_mutations((1))
    WHERE state IN ('intent', 'yaml_applied', 'recovery_hold');
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_attempt_one_active_per_job
    ON execution_attempts(job_id)
    WHERE state IN ('leased', 'dispatching', 'running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_node_attempts_execution_attempt
    ON node_attempts(execution_attempt_id)
    WHERE execution_attempt_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_execution_contract_role
    ON jobs(execution_approval_id, execution_contract_role)
    WHERE execution_approval_id IS NOT NULL
      AND execution_contract_role IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_legacy_stop_one_unresolved_per_job
    ON legacy_job_stop_intents(job_id)
    WHERE state IN ('requested', 'delivery_uncertain', 'delivered');

CREATE INDEX IF NOT EXISTS idx_execution_attempts_state
    ON execution_attempts(state, liveness);
CREATE INDEX IF NOT EXISTS idx_execution_attempts_server
    ON execution_attempts(server_name, state);
CREATE INDEX IF NOT EXISTS idx_execution_operations_state
    ON execution_operations(state, retry_at);
CREATE INDEX IF NOT EXISTS idx_execution_operations_attempt
    ON execution_operations(attempt_id, operation);
CREATE INDEX IF NOT EXISTS idx_execution_events_attempt
    ON execution_attempt_events(attempt_id, id);
CREATE INDEX IF NOT EXISTS idx_execution_shadow_tick
    ON execution_shadow_observations(tick_id, id);

CREATE TRIGGER IF NOT EXISTS approvals_execution_pin_insert_guard
BEFORE INSERT ON approvals
WHEN
    (NEW.payload_sha256 IS NOT NULL)
    OR (NEW.payload_contract_version IS NOT NULL)
    OR (NEW.payload_immutable_at IS NOT NULL)
BEGIN
    SELECT CASE WHEN
        NEW.payload_sha256 IS NULL
        OR NEW.payload_contract_version IS NULL
        OR NEW.payload_immutable_at IS NULL
    THEN RAISE(ABORT, 'approval execution pin fields must be all-or-none') END;
END;

CREATE TRIGGER IF NOT EXISTS approvals_execution_pin_update_guard
BEFORE UPDATE ON approvals
WHEN
    OLD.payload IS NOT NEW.payload
    OR OLD.payload_sha256 IS NOT NEW.payload_sha256
    OR OLD.payload_contract_version IS NOT NEW.payload_contract_version
    OR OLD.payload_immutable_at IS NOT NEW.payload_immutable_at
BEGIN
    SELECT CASE WHEN
        OLD.payload_sha256 IS NOT NULL
        OR NEW.payload_sha256 IS NOT NULL
        OR OLD.payload_contract_version IS NOT NULL
        OR NEW.payload_contract_version IS NOT NULL
        OR OLD.payload_immutable_at IS NOT NULL
        OR NEW.payload_immutable_at IS NOT NULL
    THEN RAISE(ABORT, 'pinned approval payload is immutable') END;
END;

CREATE TRIGGER IF NOT EXISTS approvals_materialization_started_guard
BEFORE UPDATE OF materialization_started_at ON approvals
WHEN NOT (
    OLD.materialization_started_at IS NULL
    AND NEW.materialization_started_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'approval materialization marker is one-way');
END;

CREATE TRIGGER IF NOT EXISTS jobs_execution_pin_insert_guard
BEFORE INSERT ON jobs
WHEN
    NEW.execution_approval_id IS NOT NULL
    OR NEW.approved_payload_sha256 IS NOT NULL
    OR NEW.execution_contract_version IS NOT NULL
    OR NEW.execution_contract_role IS NOT NULL
    OR NEW.approved_command_sha256 IS NOT NULL
BEGIN
    SELECT CASE WHEN
        NEW.execution_approval_id IS NULL
        OR NEW.approved_payload_sha256 IS NULL
        OR NEW.execution_contract_version IS NULL
        OR NEW.execution_contract_role IS NULL
        OR NEW.approved_command_sha256 IS NULL
    THEN RAISE(ABORT, 'job execution pin fields must be all-or-none') END;
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM approvals
        WHERE id = NEW.execution_approval_id
          AND payload_sha256 = NEW.approved_payload_sha256
          AND payload_contract_version = NEW.execution_contract_version
          AND status IN ('pending', 'approved')
    )
    THEN RAISE(ABORT, 'job execution approval linkage mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS jobs_execution_pin_update_guard
BEFORE UPDATE ON jobs
WHEN
    OLD.execution_approval_id IS NOT NEW.execution_approval_id
    OR OLD.approved_payload_sha256 IS NOT NEW.approved_payload_sha256
    OR OLD.execution_contract_version IS NOT NEW.execution_contract_version
    OR OLD.execution_contract_role IS NOT NEW.execution_contract_role
    OR OLD.approved_command_sha256 IS NOT NEW.approved_command_sha256
    OR (
        OLD.execution_approval_id IS NOT NULL
        AND (
            OLD.command IS NOT NEW.command
            OR OLD.type IS NOT NEW.type
            OR OLD.project IS NOT NEW.project
            OR OLD.require_tag IS NOT NEW.require_tag
            OR OLD.pin_server IS NOT NEW.pin_server
            OR OLD.depends_on IS NOT NEW.depends_on
            OR OLD.gpus_needed IS NOT NEW.gpus_needed
            OR OLD.priority IS NOT NEW.priority
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'pinned job execution contract is immutable');
END;

CREATE TRIGGER IF NOT EXISTS node_attempt_execution_link_guard
BEFORE UPDATE OF execution_attempt_id ON node_attempts
WHEN NEW.execution_attempt_id IS NOT OLD.execution_attempt_id
BEGIN
    SELECT CASE WHEN OLD.execution_attempt_id IS NOT NULL
        THEN RAISE(ABORT, 'node attempt execution link is immutable') END;
    SELECT CASE WHEN NEW.execution_attempt_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM execution_attempts
        WHERE id = NEW.execution_attempt_id
          AND job_id = NEW.job_id
          AND backend = 'node'
    )
    THEN RAISE(ABORT, 'node attempt execution link mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS node_attempt_execution_link_insert_guard
BEFORE INSERT ON node_attempts
WHEN NEW.execution_attempt_id IS NOT NULL
BEGIN
    SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM execution_attempts
        WHERE id = NEW.execution_attempt_id
          AND job_id = NEW.job_id
          AND backend = 'node'
    )
    THEN RAISE(ABORT, 'node attempt execution link mismatch') END;
END;

CREATE TRIGGER IF NOT EXISTS server_config_revision_immutable
BEFORE UPDATE ON server_config_revisions
WHEN
    OLD.id IS NOT NEW.id
    OR OLD.server_name IS NOT NEW.server_name
    OR OLD.revision IS NOT NEW.revision
    OR OLD.normalized_target_json IS NOT NEW.normalized_target_json
    OR OLD.credential_ref_json IS NOT NEW.credential_ref_json
    OR OLD.target_identity_sha256 IS NOT NEW.target_identity_sha256
    OR OLD.assignment_eligibility IS NOT NEW.assignment_eligibility
    OR OLD.created_by_approval_id IS NOT NEW.created_by_approval_id
    OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'server config revision identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS server_config_revision_transition_guard
BEFORE UPDATE OF publication_state, activated_at, retired_at
    ON server_config_revisions
WHEN NOT (
    (OLD.publication_state = 'prepared'
     AND NEW.publication_state IN ('active', 'retired'))
    OR
    (OLD.publication_state = 'active'
     AND NEW.publication_state = 'retired')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid server config revision transition');
END;

CREATE TRIGGER IF NOT EXISTS server_config_mutation_immutable
BEFORE UPDATE ON server_config_mutations
WHEN
    OLD.id IS NOT NEW.id
    OR OLD.approval_id IS NOT NEW.approval_id
    OR OLD.operation IS NOT NEW.operation
    OR OLD.server_name IS NOT NEW.server_name
    OR OLD.prior_revision_id IS NOT NEW.prior_revision_id
    OR OLD.prepared_revision_id IS NOT NEW.prepared_revision_id
    OR OLD.approved_payload_sha256 IS NOT NEW.approved_payload_sha256
    OR OLD.yaml_before_sha256 IS NOT NEW.yaml_before_sha256
    OR OLD.yaml_after_sha256 IS NOT NEW.yaml_after_sha256
    OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'server config mutation identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS server_config_mutation_transition_guard
BEFORE UPDATE OF state ON server_config_mutations
WHEN NOT (
    (OLD.state = 'intent'
     AND NEW.state IN ('yaml_applied', 'rolled_back', 'recovery_hold'))
    OR
    (OLD.state = 'yaml_applied'
     AND NEW.state IN ('activated', 'rolled_back', 'recovery_hold'))
    OR
    (OLD.state = 'recovery_hold'
     AND NEW.state IN ('yaml_applied', 'rolled_back'))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid server config mutation transition');
END;

CREATE TRIGGER IF NOT EXISTS execution_attempt_immutable
BEFORE UPDATE ON execution_attempts
WHEN
    OLD.id IS NOT NEW.id
    OR OLD.job_id IS NOT NEW.job_id
    OR OLD.attempt_number IS NOT NEW.attempt_number
    OR OLD.backend IS NOT NEW.backend
    OR OLD.server_name IS NOT NEW.server_name
    OR OLD.server_config_revision_id IS NOT NEW.server_config_revision_id
    OR OLD.target_identity_sha256 IS NOT NEW.target_identity_sha256
    OR OLD.execution_approval_id IS NOT NEW.execution_approval_id
    OR OLD.approved_payload_sha256 IS NOT NEW.approved_payload_sha256
    OR OLD.execution_contract_version IS NOT NEW.execution_contract_version
    OR OLD.fencing_token IS NOT NEW.fencing_token
    OR OLD.scheduler_fencing_epoch IS NOT NEW.scheduler_fencing_epoch
    OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'execution attempt identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS execution_operation_immutable
BEFORE UPDATE ON execution_operations
WHEN
    OLD.id IS NOT NEW.id
    OR OLD.attempt_id IS NOT NEW.attempt_id
    OR OLD.operation IS NOT NEW.operation
    OR OLD.idempotency_key IS NOT NEW.idempotency_key
    OR OLD.authorization_approval_id IS NOT NEW.authorization_approval_id
    OR OLD.authorization_class IS NOT NEW.authorization_class
    OR OLD.authorized_contract_sha256 IS NOT NEW.authorized_contract_sha256
    OR OLD.payload_json IS NOT NEW.payload_json
    OR OLD.payload_sha256 IS NOT NEW.payload_sha256
    OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'execution operation identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS legacy_job_stop_intent_immutable
BEFORE UPDATE ON legacy_job_stop_intents
WHEN
    OLD.id IS NOT NEW.id
    OR OLD.job_id IS NOT NEW.job_id
    OR OLD.stop_approval_id IS NOT NEW.stop_approval_id
    OR OLD.approved_payload_sha256 IS NOT NEW.approved_payload_sha256
    OR OLD.backend IS NOT NEW.backend
    OR OLD.server_name IS NOT NEW.server_name
    OR OLD.requested_at IS NOT NEW.requested_at
BEGIN
    SELECT RAISE(ABORT, 'legacy stop intent identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS server_config_revisions_no_delete
BEFORE DELETE ON server_config_revisions
BEGIN SELECT RAISE(ABORT, 'server config revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS server_config_mutations_no_delete
BEFORE DELETE ON server_config_mutations
BEGIN SELECT RAISE(ABORT, 'server config mutations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_attempts_no_delete
BEFORE DELETE ON execution_attempts
BEGIN SELECT RAISE(ABORT, 'execution attempts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_operations_no_delete
BEFORE DELETE ON execution_operations
BEGIN SELECT RAISE(ABORT, 'execution operations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_attempt_events_no_update
BEFORE UPDATE ON execution_attempt_events
BEGIN SELECT RAISE(ABORT, 'execution attempt events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_attempt_events_no_delete
BEFORE DELETE ON execution_attempt_events
BEGIN SELECT RAISE(ABORT, 'execution attempt events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_shadow_observations_no_update
BEFORE UPDATE ON execution_shadow_observations
BEGIN SELECT RAISE(ABORT, 'execution shadow observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS execution_shadow_observations_no_delete
BEFORE DELETE ON execution_shadow_observations
BEGIN SELECT RAISE(ABORT, 'execution shadow observations are append-only'); END;
CREATE TRIGGER IF NOT EXISTS legacy_job_stop_intents_no_delete
BEFORE DELETE ON legacy_job_stop_intents
BEGIN SELECT RAISE(ABORT, 'legacy stop intents are append-only'); END;

-- WP-2B (DG-AMBIGUOUS-LAUNCH-v1 §7). A fresh DB gets the enumerations as
-- CHECK constraints in the CREATE TABLE above, but `ALTER TABLE ADD COLUMN`
-- cannot carry a CHECK, so a migrated legacy DB would otherwise accept any
-- value. These triggers bind both paths identically.
CREATE TRIGGER IF NOT EXISTS execution_attempts_launch_domain_insert
BEFORE INSERT ON execution_attempts
BEGIN
    SELECT CASE WHEN
        NEW.remote_claim_state IS NOT NULL
        AND NEW.remote_claim_state NOT IN (
            'unclaimed', 'launcher_claimed', 'controller_abandoned',
            'claim_unknown')
    THEN RAISE(ABORT, 'invalid remote_claim_state') END;
    SELECT CASE WHEN
        NEW.prelaunch_verdict IS NOT NULL
        AND NEW.prelaunch_verdict NOT IN ('definite_not_launched', 'ambiguous')
    THEN RAISE(ABORT, 'invalid prelaunch_verdict') END;
END;

CREATE TRIGGER IF NOT EXISTS execution_attempts_launch_domain_update
BEFORE UPDATE OF remote_claim_state, prelaunch_verdict ON execution_attempts
BEGIN
    SELECT CASE WHEN
        NEW.remote_claim_state IS NOT NULL
        AND NEW.remote_claim_state NOT IN (
            'unclaimed', 'launcher_claimed', 'controller_abandoned',
            'claim_unknown')
    THEN RAISE(ABORT, 'invalid remote_claim_state') END;
    SELECT CASE WHEN
        NEW.prelaunch_verdict IS NOT NULL
        AND NEW.prelaunch_verdict NOT IN ('definite_not_launched', 'ambiguous')
    THEN RAISE(ABORT, 'invalid prelaunch_verdict') END;
END;

-- A controller-won claim is the one piece of evidence that lets a Job go back
-- to queued, so it must never be walked back into a launched state.
CREATE TRIGGER IF NOT EXISTS execution_attempts_claim_is_one_way
BEFORE UPDATE OF remote_claim_state ON execution_attempts
WHEN OLD.remote_claim_state IN ('launcher_claimed', 'controller_abandoned')
    AND NEW.remote_claim_state IS NOT OLD.remote_claim_state
BEGIN
    SELECT RAISE(ABORT, 'settled remote claim state is immutable');
END;

-- The receipt digest and the boot id it was observed under are evidence. Once
-- recorded they pin what was seen; a later read may not rewrite history.
CREATE TRIGGER IF NOT EXISTS execution_attempts_receipt_evidence_is_immutable
BEFORE UPDATE OF launch_receipt_sha256, remote_boot_id ON execution_attempts
WHEN (OLD.launch_receipt_sha256 IS NOT NULL
      AND NEW.launch_receipt_sha256 IS NOT OLD.launch_receipt_sha256)
   OR (OLD.remote_boot_id IS NOT NULL
       AND NEW.remote_boot_id IS NOT OLD.remote_boot_id)
BEGIN
    SELECT RAISE(ABORT, 'recorded launch evidence is immutable');
END;

CREATE TRIGGER IF NOT EXISTS execution_operations_transmission_domain_insert
BEFORE INSERT ON execution_operations
BEGIN
    SELECT CASE WHEN
        NEW.transmission_state IS NOT NULL
        AND NEW.transmission_state NOT IN (
            'not_transmitted', 'transmitted', 'unknown')
    THEN RAISE(ABORT, 'invalid transmission_state') END;
END;

CREATE TRIGGER IF NOT EXISTS execution_operations_transmission_domain_update
BEFORE UPDATE OF transmission_state ON execution_operations
BEGIN
    SELECT CASE WHEN
        NEW.transmission_state IS NOT NULL
        AND NEW.transmission_state NOT IN (
            'not_transmitted', 'transmitted', 'unknown')
    THEN RAISE(ABORT, 'invalid transmission_state') END;
    -- Deliberately no "effect_started_at implies not not_transmitted" rule
    -- here.  Controller arbitration establishes non-transmission precisely
    -- *after* the launch effect started -- winning the remote claim is what
    -- proves the launcher can never run -- so a static trigger asserting the
    -- opposite would forbid the mechanism this gate exists to provide.
    -- The real protection is the abandon guard in
    -- `transition_execution_attempt`, which refuses `abandoned_before_launch`
    -- while a launch effect started and has not been proven non-transmitted.
END;

-- A published snapshot is evidence. Rewriting its identity would silently
-- redefine what every run that referenced it actually consumed.
-- A plan is approved by its digest, so the digest and everything it covers
-- must never change after the row exists.
CREATE TRIGGER IF NOT EXISTS execution_plans_are_immutable
BEFORE UPDATE OF plan_digest, command_sha256, project_version_id,
    run_profile_id, dataset_snapshot_id, dataset_none,
    server_config_revision_id, reproducible, contract_version
ON execution_plans
BEGIN
    SELECT RAISE(ABORT, 'execution plan is immutable');
END;

CREATE TRIGGER IF NOT EXISTS execution_plans_no_delete
BEFORE DELETE ON execution_plans
BEGIN SELECT RAISE(ABORT, 'execution plans are append-only'); END;

CREATE TRIGGER IF NOT EXISTS dataset_snapshots_published_is_immutable
BEFORE UPDATE ON dataset_snapshots
WHEN OLD.state = 'published'
BEGIN
    SELECT RAISE(ABORT, 'published dataset snapshot is immutable');
END;

CREATE TRIGGER IF NOT EXISTS dataset_snapshots_no_delete
BEFORE DELETE ON dataset_snapshots
BEGIN SELECT RAISE(ABORT, 'dataset snapshots are append-only'); END;

CREATE TRIGGER IF NOT EXISTS dataset_snapshot_shards_no_update
BEFORE UPDATE ON dataset_snapshot_shards
BEGIN SELECT RAISE(ABORT, 'dataset snapshot shards are append-only'); END;

CREATE TRIGGER IF NOT EXISTS dataset_snapshot_shards_no_delete
BEFORE DELETE ON dataset_snapshot_shards
BEGIN SELECT RAISE(ABORT, 'dataset snapshot shards are append-only'); END;

CREATE TRIGGER IF NOT EXISTS server_config_revisions_preflight_domain
BEFORE UPDATE OF attempt_backend_preflight ON server_config_revisions
BEGIN
    SELECT CASE WHEN
        NEW.attempt_backend_preflight IS NOT NULL
        AND NEW.attempt_backend_preflight NOT IN (
            'eligible', 'ineligible_non_local_fs', 'unknown')
    THEN RAISE(ABORT, 'invalid attempt_backend_preflight') END;
END;
"""
