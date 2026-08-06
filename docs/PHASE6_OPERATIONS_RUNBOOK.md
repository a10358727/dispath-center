# Phase 6 Operations Runbook

> Status: code/runbook evidence only. This document does not claim deployment,
> canary, restore-drill or production readiness.
>
> `DG-OPS-SLO` is still unapproved. Therefore backup age is measured but has no
> alert/readiness threshold, the timer is a disabled template, and no retention
> deletion is automatic.

## 1. Process topology

`PROCESS_ROLE` has three fail-closed values:

| Role | Serves API | Starts monitor/scheduler/maintenance loops |
|---|---:|---:|
| `all` | yes | yes |
| `api` | yes | no |
| `scheduler` | yes, for health/admin | yes |

The compatible first deployment is one `role=all` process. A split deployment
may run multiple `api` processes and one `scheduler` process against the same
SQLite state. While all durable-attempt rollout flags are false, do not run
multiple scheduler processes: the legacy scheduler predates lease fencing.

When durable ownership is enabled, every scheduler contender competes for the
SQLite leader lease. A losing process skips the entire scheduler tick, including
the legacy branch; it may still serve health and administrative reads. The
current owner/fencing epoch is visible in:

```text
GET /execution-control/status
GET /operations/metrics
GET /readyz
```

All three routes are authenticated `PLATFORM_VIEW` surfaces. They were not made
public because the authentication exemption set is a protected invariant.

## 2. Leader takeover drill

Run this only with a temporary/non-production database and workers.

1. Start two `PROCESS_ROLE=scheduler` contenders against the same temporary
   SQLite file, with the durable ownership/reconcile/outbox flags enabled.
2. Verify exactly one reports
   `execution.ownership.current_process_is_leader=true`; the other must skip
   scheduler work.
3. Record active Job, attempt and operation IDs.
4. Stop the leader. Do not edit the lease row or system clock.
5. After the old lease expires, verify the contender acquires a higher fencing
   epoch.
6. Verify the pre-existing attempts were reconciled under their original IDs;
   they must not be rebuilt or relaunched.
7. Exercise a delayed old-owner write and confirm the SQLite fencing predicate
   rejects it.
8. Return to one scheduler and the SSH/default-off configuration.

Local deterministic coverage:

```bash
DISPATCH_TEST_NETWORK=deny .venv/bin/python -m pytest -q \
  tests/test_execution_attempt_foundation.py \
  tests/test_execution_attempt_dispatch.py \
  tests/test_health_endpoints.py
```

These tests cover lease expiry/takeover, stale fencing epochs, non-leader
scheduler refusal and health semantics. They do not replace a real takeover
drill.

## 3. Health and metrics

`GET /healthz` proves only that the process/event loop responds. `GET /readyz`
checks schema, writable state path, role configuration, critical-loop freshness
and unexpected supervised-loop exits. A non-leader remains ready for API/read
traffic.

`GET /operations/metrics` returns:

- queue depth/oldest age, attempts by state/backend, unknown age;
- outbox/collection backlog, four-operation Node completion backlog/failures
  and uncertain age;
- leader owner/fencing state and rollout flags;
- loop tick/error counts, last safe error category and unexpected exits;
- Node liveness, queue depth, stale attempts and attention count;
- SQLite size and state-filesystem capacity;
- latest configured backup age and checksum-metadata presence.

The `audit.export_outbox` object includes a shared alert projection. A backlog
sets `status=attention`; a dead-letter row sets `alert.active=true` with
`severity=critical` and reason `dead_letter_present`. This is a deterministic
local signal for an operator/monitor to consume, not an automatic replay or a
production notification policy. Configure the external alert owner and SLO
before treating it as a release gate.

The endpoint never changes Job/attempt/Node state and does not infer failure
from missing heartbeats or unreachable remote state.

For a point-in-time audit export gate against a database or restored copy, use
the read-only checker:

```bash
.venv/bin/python scripts/audit_export_status.py \
  --db /path/to/jobqueue.db --require-clear --json
```

It exits non-zero when pending/failed/processing backlog or dead-letter rows
exist. It never claims that a point-in-time result is a continuous alert or a
DG-OPS-SLO readiness decision; schedule it only after an operator-approved
threshold and alert owner exist.

For the durable execution-attempt and Node terminal-completion outboxes, use
the separate read-only evidence gate:

```bash
.venv/bin/python scripts/execution_outbox_status.py \
  --db /path/to/jobqueue.db --require-clear --json
```

The report includes both tables' state/operation counts, unresolved
`pending`/`processing`/`uncertain` work, processing/uncertain age samples and
terminal `failed` counts. It does not claim work, run migrations, contact
SSH/Node or infer remote state. `--require-clear` is a point-in-time operator
precondition only; it is not a production SLO or canary result.

## 4. Backup

Set `BACKUP_ROOT` to a protected mount or replicated directory that is not on
the single Server A filesystem. The automatic unit refuses to run when the
value is absent:

```bash
BACKUP_ROOT=/mounted/off-host/dispatch-backups deploy/backup.sh
```

Each invocation refuses a missing source database and reserves a new exclusive
destination, so concurrent or same-second runs cannot merge. It uses SQLite
online backup, copies audit/configuration and Hub state, optionally archives
datasets/results, creates `CHECKSUMS.sha256`, then pins that inventory digest
in `MANIFEST`. Every generated archive is validated before publication.

Restore first verifies both digest layers, rejects absolute/traversal paths,
links, devices and duplicate archive members, and safely extracts the complete
backup into staging. It prompts and moves current state only after that entire
staging pass succeeds.

Templates:

- `deploy/dispatch-center-backup.service`
- `deploy/dispatch-center-backup.timer`

Replace every placeholder before installation. The timer contains the draft
daily cadence but is not enabled by repository code. If `DG-OPS-SLO` changes
the RPO, edit `OnCalendar` before activation.

## 5. Restore drill

Do not run the destructive restore script for a routine drill. Use:

```bash
.venv/bin/python scripts/restore_drill.py \
  --backup-dir /mounted/off-host/dispatch-backups/<stamp> \
  --source /path/to/live/jobqueue.db
```

When a signed checkpoint and its independent key are available, include the
anchor and the exact backup manifest so the drill verifies the restored audit
chain boundary and manifest binding as part of the same PASS/FAIL result:

```bash
.venv/bin/python scripts/restore_drill.py \
  --backup-dir /mounted/off-host/dispatch-backups/<stamp> \
  --source /path/to/live/jobqueue.db \
  --audit-anchor /mounted/off-host/audit-checkpoint.json \
  --signing-key-file /run/secrets/audit-checkpoint.key \
  --backup-manifest /mounted/off-host/dispatch-backups/<stamp>/MANIFEST
```

Record the date, backup creation time, measured restore duration, integrity
result, per-table row counts, Git-ref verification and sampled result/artifact
metadata in `docs/IMPLEMENTATION_PROGRESS.md`. When supplied, the signed
checkpoint is also verified against the restored database; a bad signature,
wrong key or manifest mismatch makes the drill fail closed. The full-directory
drill verifies both checksum layers, restores all included archives into a
temporary directory, enumerates Git refs and hashes a bounded result sample;
it does not modify either input.

`--backup /path/to/jobqueue.db` remains available as a database-only
diagnostic, but it is not a complete Phase 6 restore drill and cannot establish
whole-state RTO.

`deploy/restore.sh` is for an actual operator-approved restore after the service
is stopped. It preserves displaced state and refuses a checksum or unsafe
archive before touching the destination.

## 6. What still needs a decision or external evidence

- `DG-OPS-SLO`: numeric RPO/RTO, backup-age alert, drill cadence, retention and
  the evidence required for `production-ready=yes`.
- WP-2D v2: non-production SSH canary for at least 20 Jobs/8 hours.
- `DG-NODE-CANARY` and Phase 5: two real Nodes, 100 Jobs and seven days.
- A real off-host backup plus a recorded restore/takeover drill.
