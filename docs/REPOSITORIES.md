# Repository and Unit of Work boundary

PR-07 introduces the additive persistence seam at
`dispatch_center.infrastructure.db`:

- `protocols.py` defines the Node, canonical Execution, and durable Audit
  repository contracts.
- `sqlite.py` provides `SQLiteNodeRepository`, `SQLiteExecutionRepository`,
  `SQLiteAuditRepository`, and `SQLiteUnitOfWork` adapters.
- `Database.transaction()` is the explicit rollback boundary for a future
  application service that updates multiple repositories together.

The adapters currently delegate to the existing `app.db.Database` methods.
Those methods already own the tested `BEGIN IMMEDIATE` state-transition guards,
so this step does not duplicate or weaken execution evidence. New Node/attempt
callers use the UoW seam; older callers remain supported through the facade.

`SQLiteUnitOfWork.run(callback)` requires an entered UoW, rejects nested
transactions, and rolls back the complete callback on any exception. The
transaction-local SQLite cursor lets a migrated facade operation join the same
boundary; `uow.audit.append(cursor, ...)` writes the hash-chained
`audit_events` row and export intent before commit. Separate legacy facade
calls remain independently atomic until their call sites are migrated and
tested.

PR-08 adds `audit_events` and `audit_export_operations` through schema
migrations 2–3. The v1 hash contract is versioned and old v0 rows remain
verifiable without rewriting them. JSONL remains an at-least-once
export/compatibility sink, while `/events` and `/audit` mark each row's source
and durability and expose the partial adoption catalog. Export claims use an
atomic compare-and-swap lease; failures retry and eventually enter
`dead_letter`, which only an explicit operator replay can reopen. They never
delete the DB event, and verified backups validate the chain before rollback
use. A production external checkpoint anchor is still required for authenticity.
