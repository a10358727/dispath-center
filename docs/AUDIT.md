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

The machine-readable catalog is `app.audit_adoption.AUDIT_ADOPTION`. It records
the two migrated execution creation paths as durable and explicitly records
the still-legacy project and approval mutation families. No owner or issue
identifiers are invented when tracking metadata is not present;
`entries_without_owner` and `legacy_without_migration_issue` make those review
gaps machine-readable. New mutation slices must add a catalog entry and must
not double-write a durable action to the legacy JSONL writer.

## Integrity limits and production gates

A hash chain proves internal consistency, not that an attacker could not
rewrite the entire SQLite file and recompute the chain. Production deployment
therefore still requires an external checkpoint anchor (for example, the last
event hash per day or segment in off-host immutable storage, with an
independent signature). Retention must archive segments with first/last hash,
the previous segment hash, checksum/signature, and policy metadata; deleting
individual chain rows is not supported.

Backup restore verifies SQLite integrity and the internal chain, but it does
not satisfy the external-anchor gate. Full-domain audit adoption, approval and
identity migration, and idle-host full-suite/quickstart repetition remain
open review items.
