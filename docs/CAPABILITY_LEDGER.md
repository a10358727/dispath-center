# Capability Ledger

> Updated: 2026-08-02
> Authority: this is the single current capability-status ledger referenced by
> `docs/NEXT_IMPLEMENTATION_PLAN.md`. Protected behavior is still governed by
> canonical invariants and approved decisions; this ledger cannot authorize a
> feature or change an invariant.
>
> Historical roadmap/status documents are evidence, not current capability
> claims. When they disagree with this table, use the authority order:
> canonical invariants → `docs/DECISIONS.md` → code/tests → this ledger →
> historical roadmap/status text.

## Field meanings

- `implemented`: corresponding runtime code or operator artifact exists.
- `test-only`: the capability is deliberately limited to fake/local test
  evidence and is not an operable end-to-end surface.
- `default-enabled`: a clean configuration activates it without an opt-in flag
  or operator action.
- `deployed`: this repository has direct evidence that the capability is
  currently installed in a runtime environment. `unknown` is not `yes`.
- `canary-proven`: the plan's real-environment/time-window gate has passed.
  Local tests are never substituted for canary evidence.
- `production-ready`: all required implementation, deployment, canary,
  recovery and safety gates for that capability are satisfied.

Allowed values are `yes`, `no`, `unknown`, and `n/a`. A code-complete feature
may still correctly have `production-ready=no`.

## Current ledger

| Capability ID | implemented | test-only | default-enabled | deployed | canary-proven | production-ready | Evidence / blocker |
|---|---|---|---|---|---|---|---|
| `phase0_release_gate` | yes | no | yes | unknown | n/a | no | Local Python 3.10 exact-lock check, compile, static invariant checks, diff check and the current 3,353-test offline suite pass; the CI workflow has not yet produced remote run evidence for this worktree. |
| `core_control_plane` | yes | no | yes | unknown | unknown | no | FastAPI/SQLite monitor, scheduler, approvals and APIs exist. WP-1C removed the false-terminal stop defect; WP-2A adds default-off single-leader ownership. WP-6A adds explicit `all/api/scheduler` roles, full scheduler refusal for a losing durable-lease contender, supervised readiness and evidence-backed JSON operational metrics. Ambiguous launch, default-off rollout and missing real drills still prevent a production-ready claim. |
| `approval_and_audit` | yes | no | yes | unknown | unknown | no | Approval/audit boundaries are tested; legacy dataset registration is still a direct material mutation. |
| `ssh_execution_v1` | yes | no | yes | unknown | unknown | no | Existing SSH/SFTP/tmux/sentinel backend remains the supported default. Legacy stop preserves its immutable intent/terminal-evidence semantics; the rollout-gated generic attempt path now adds durable attempt identity, prepare/launch/stop/collect operations, and evidence-driven ambiguity handling. |
| `codex_exec_runner_v1` | yes | no | no | unknown | unknown | no | Approval-gated Codex exec path exists but requires configured Runner infrastructure and has no current deployment/canary evidence. |
| `oidc_identity` | yes | no | no | no | no | no | Dependency-complete local implementation; default off and explicitly recorded as not deployed. |
| `authorization_shadow` | yes | no | no | no | no | no | Only `off`/`shadow` exist; enforcement is a separate decision gate. |
| `server_config_management` | yes | no | yes | unknown | unknown | no | All four public add/update/disable/delete request paths now pin exact canonical post-mutation YAML plus before/after digests and materialize through the immutable revision/journal protocol. A never-published legacy entry becomes revision 1 only through a fresh exact `server_update` human approval; migration does not backfill it, and retired/prepared history cannot be resurrected as legacy. Publication verifies the written digest before reload/activation; reload failure compensates only when the exact prior bytes are known, otherwise recovery stays fail-closed. Historical unpinned approvals remain honestly legacy. No deployment/canary evidence exists for these changes. |
| `mutable_dataset_registry` | yes | no | yes | unknown | unknown | no | Legacy `POST /datasets` directly persists a mutable registry row without an approval. |
| `immutable_dataset_snapshot` | yes | yes | no | no | no | no | `DG-DATASET-SNAPSHOT-v1` approved 2026-07-27. WP-3A now has a default-off local request→human approval→building→content-addressed publish workflow (`POST /datasets/{name}/{version}/snapshot-request`, `GET /dataset-snapshots*`) with atomic DB evidence, concurrent-winner fencing, source re-verification and approved-build resume. ExecutionPlan pins one published snapshot or explicit `dataset_none`; no production dataset/canary evidence exists. |
| `run_profile_v1` | yes | yes | no | no | no | no | Additive immutable revisions are consumed by ExecutionPlan: each plan pins an exact `run_profiles.id` revision and approve-time revalidation rejects missing/archived revisions. No production deployment evidence exists. |
| `auto_placement` | yes | no | no | unknown | unknown | no | Proposal and policy-scoped decision code exists behind default-off switches; no current deployment/canary evidence. |
| `backup_restore` | yes | no | no | unknown | no | no | Online backup refuses a missing database, reserves an exclusive destination, and creates per-artifact SHA-256 inventory pinned by `MANIFEST`. Restore verifies and safely stages the entire backup before displacement; traversal, absolute paths, links, devices and duplicate archive members fail closed. A disabled systemd oneshot/timer template requires explicit `BACKUP_ROOT`, while metrics report newest backup age with no unapproved threshold. `scripts/restore_drill.py` restores a copy, checks integrity/row counts and measures duration. No unit is installed/enabled, no off-host backup or real drill evidence exists, and numeric RPO/RTO/retention still need `DG-OPS-SLO`. |
| `node_protocol_v1` | yes | yes | no | no | no | no | Compatibility protocol remains available behind the protocol/drain flag; v2 request validation rejects agent-supplied `job_id`. |
| `node_daemon` | yes | yes | no | no | no | no | `agent/__main__.py` is runnable. It fsyncs a local identity/digest/`not_launched` journal before ack, treats response-loss as unknown, and on every reconnect recovers the exact current attempt before polling. A first launch resumes only with exact attempt/payload and local no-launch evidence. It uses bounded backoff/jitter and explicit process-group isolation, records stop receipts without fabricating terminals, never relaunches an acknowledged/intent-marked attempt, monitors child exits, durably retries bounded log-tail and explicit artifact metadata reports independently of terminal reports, and opens no listener. Real activation still requires `DG-NODE-CANARY`. |
| `node_protocol_v2` | yes | yes | no | no | no | no | `DG-NODE-V2-v1` ruled 2026-07-28. Server-selected lease atomically creates linked generic/protocol attempts; ack projects the Job to running, stale heartbeat changes liveness only to unknown, and terminal converges both attempts plus the canonical Job. Terminal also atomically appends exactly four durable completion operations. Exact target/credential/key identity is revalidated before material effects. Staged rotation keeps bounded overlap; routine retirement drains while emergency revoke marks no work failed. The strict Phase 5 evaluator checks exact bindings, terminal coverage and completion delivery. Flags stay off, no node is enrolled, and real-machine activation needs `DG-NODE-CANARY`. |
| `execution_attempt_outbox` | yes | yes | no | no | no | no | Attempt claim + first prepare intent are one DB transaction; owner-only delivery covers pinned prepare/launch/stop/collect operations with an effect boundary. Exact companion/receipt/tmux/sentinel evidence settles an uncertain launch without replay, while a controller-won claim records definite non-transmission before requeue. Terminal attempts inherited from older code get evidence-only recovery without repeated hooks. Public generic-attempt stop and terminal collect are wired. Node terminal additionally creates an immutable exactly-four completion bundle (`dependency_refresh`, `result_collection`, `notification`, `owner_projection`) with fenced claim, durable outcome and restart recovery. Every rollout flag remains default-off; no production migration/canary proof exists. |
| `attempt_driven_ssh` | yes | yes | no | no | no | no | WP-2C runtime dispatch is wired into the scheduler with active-attempt exclusion, immutable revision identity checks, durable prepare/launch intents and D-5 revision-scoped filesystem evidence. The first real forced-response-loss drill found and prompted repair of a launch-operation convergence gap, but candidate `74f76aae...` failed that criterion and is not canary-proven. A fixed read-only SSH preflight records local/NFS/unknown on the exact active revision; scheduler selection and the DB claim transaction require `eligible`. SSH fallback remains the default. The repaired candidate must restart the full `DG-WP2D-CANARY-v2` 8-hour window. |
| `code_promotion_v1` | yes | no | no | no | no | no | Default-off public request/UI and manual approval path resolve only a terminal native Engineering Task with a verified canonical local-result bundle. Approval rechecks artifact bytes, performs real `git bundle verify` in staging, creates a non-runnable ProjectVersion, publishes an exact local Hub ref, then atomically marks it promoted. Interrupted publication is retryable; retirement preserves evidence. No GitHub push, worktree deletion, deployment or canary evidence exists. |
| `immutable_execution_plan` | yes | no | no | no | no | no | Ready plan and pinned pending approval are created in one SQLite transaction with immutable linkage. Approval materializes at most one target-pinned Job, and attempt creation revalidates plan/job/target digests. `GET /runs/{plan_id}` returns the durable plan→approval→Job→attempt→operation/event/node-artifact lineage without inferring failure from absent evidence. No deployment/canary evidence exists. |
| `codex_app_server_adapter` | yes | yes | no | no | no | no | Bounded fake protocol adapter exists; production wiring/version pin/canary remain separately gated. |
| `github_publication` | yes | yes | no | no | no | no | Interface and fake only; no production adapter or approved credential/egress configuration. |

## Release blockers carried forward

| Blocker | Current evidence | Required destination |
|---|---|---|
| `RB-LAUNCH-001` | **Repaired after a failed live criterion; not yet proven.** Candidate `74f76aae...` preserved one attempt and recovered liveness during forced response loss, but failed to settle its launch operation from uncertain. The new code requires exact remote evidence, converges the same operation without replay, and retains default-off fallback. | Restart WP-2D v2 from a new exact candidate on the non-production worker (≥20 jobs / 8h, forced response-loss, restart and rollback), then evaluate stable evidence. |
| `RB-NODE-001` | v2 daemon/protocol, linked generic-attempt lifecycle, stale-heartbeat unknown handling, exactly-four durable completion operations, reconnect recovery, staged rotation and drain/revoke split are implemented and locally tested; no real node evidence exists. | `DG-NODE-CANARY` plus Phase 5 (2 nodes / 100 jobs / 7 days); keep production workers on SSH. |

## Resolved blockers

| Blocker | Resolution evidence |
|---|---|
| `RB-DATASET-001` | `DG-DATASET-SNAPSHOT-v1` D-1 kept `POST /datasets` as an approval-free registry declaration and labelled it instead: `datasets.reproducible` is `0` for every row and no migration sets it to 1. D-5's rejection is now enforced on the request path — `POST /projects/{name}/runs/request` returns 400 `dataset_not_reproducible` and persists nothing, verified end-to-end through the real app in `tests/test_execution_plan_api.py`. The reproducible alternative (`dataset_snapshot`) exists alongside. |
| `RB-SERVER-001` | All four approved server mutations publish through the protocol; unpinned historical approvals stay honestly `legacy_observed`. A never-published legacy target needs a fresh exact update approval to create revision 1; no history is fabricated. Digest drift, failed writes, reload failure and an unknown disk state each have an explicit outcome, and the operator recovery surface cannot fabricate a revision or edit a digest. Current full offline suite: 3,353 passed. |
| `RB-STOP-001` | WP-1C pins new stop approvals, persists an immutable legacy intent before delivery, never projects kill success/failure to terminal, blocks requeue/re-dispatch while unresolved, and closes only with terminal evidence. `tests/test_approvals.py::test_stop_kill_failure_stays_running_with_uncertain_intent` plus scheduler crash/requeue/terminal tests pass in the 2,951-test full suite. |

## Updating this ledger

Every work package must update its affected rows and blockers. A field moves to
`yes` only with direct evidence recorded in `docs/IMPLEMENTATION_PROGRESS.md`.
Missing deployment/canary evidence stays `unknown` or `no`; it is never inferred
from passing unit tests.
