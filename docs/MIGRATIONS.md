# SQLite migrations

The control plane keeps its existing SQLite facade, but schema changes now go
through the ordered ledger in `schema_migrations`. Version 1 is the additive
compatibility migration that covers the historical `ALTER TABLE` columns,
project UUID backfill, and supporting indexes. Version 2 adds the durable
`audit_events` ledger and its export outbox. Version 3 adds the hash contract
discriminator while preserving v0 hashes already written by the first audit
foundation. Existing values are preserved; no approval, revision, digest, or
execution history is fabricated, and legacy JSONL lines are not backfilled
into the new hash chain.

The checked-in target is schema version 3; `schema_is_initialized()` fails
closed until the ledger, hash-version column, durable audit tables, and export
outbox are all present.

The migration runner also mirrors the applied version in SQLite's
`PRAGMA user_version`, takes a process-level lock beside the database, and
wraps each migration step and ledger write in a transaction. A failed step can
be retried; its ledger row is not recorded.

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
```

The deployment order is backup, `check`, `upgrade`, application rollout, and
smoke test. `current` and `check` do not instantiate the application. The
legacy `Database(path)` compatibility constructor still performs the same
idempotent versioned upgrade used by existing local/test callers; new
deployments should run the explicit CLI preflight before starting the service.

`backup` uses SQLite's online backup API and never mutates the source. A
restored copy must pass `PRAGMA integrity_check` and the durable audit
hash-chain check before it is considered a rollback input. This check proves
internal consistency only; production still needs an off-host immutable
checkpoint anchor. There is no destructive down-migration; rollback is
performed by restoring a verified backup and reinstalling the prior
application wheel.
