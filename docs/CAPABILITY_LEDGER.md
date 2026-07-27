# Capability Ledger

> Updated: 2026-07-27  
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
| `phase0_release_gate` | yes | no | yes | unknown | n/a | no | Local clean Python 3.10 hash install, smoke/static gates and the current 2,955-test suite pass; the new CI workflow has not yet produced remote run evidence. |
| `core_control_plane` | yes | no | yes | unknown | unknown | no | FastAPI/SQLite monitor, scheduler, approvals and APIs exist. WP-1C removed the false-terminal stop defect; WP-2A adds default-off single-leader ownership and read-only execution telemetry. Ambiguous launch and other blockers below still prevent a production-ready claim. |
| `approval_and_audit` | yes | no | yes | unknown | unknown | no | Approval/audit boundaries are tested; legacy dataset registration is still a direct material mutation. |
| `ssh_execution_v1` | yes | no | yes | unknown | unknown | no | Existing SSH/SFTP/tmux/sentinel backend remains the supported default. Stop now persists an immutable intent and waits for terminal evidence; durable attempt identity and ambiguous-launch closure are still absent. |
| `codex_exec_runner_v1` | yes | no | no | unknown | unknown | no | Approval-gated Codex exec path exists but requires configured Runner infrastructure and has no current deployment/canary evidence. |
| `oidc_identity` | yes | no | no | no | no | no | Dependency-complete local implementation; default off and explicitly recorded as not deployed. |
| `authorization_shadow` | yes | no | no | no | no | no | Only `off`/`shadow` exist; enforcement is a separate decision gate. |
| `server_config_management` | yes | no | yes | unknown | unknown | no | Legacy add/update/disable/delete remain approval-gated. WP-1A adds immutable revision/journal primitives; WP-1B now blocks delete/repoint/rotation on generic/legacy/Node ownership and prevents legacy HTTP mutations from bypassing an unresolved journal. Legacy public approvals still do not publish an approved immutable revision, so generic claims remain disabled. |
| `mutable_dataset_registry` | yes | no | yes | unknown | unknown | no | Legacy `POST /datasets` directly persists a mutable registry row without an approval. |
| `immutable_dataset_snapshot` | no | no | no | no | no | no | No content-addressed immutable snapshot publish contract; requires `DG-DATASET-SNAPSHOT` and WP-3A. |
| `run_profile_v1` | yes | no | no | no | no | no | Additive revision/API implementation exists behind a flag but is not wired into ordinary execution plans. |
| `auto_placement` | yes | no | no | unknown | unknown | no | Proposal and policy-scoped decision code exists behind default-off switches; no current deployment/canary evidence. |
| `backup_restore` | yes | no | no | unknown | no | no | Operator scripts and temporary round-trip smoke exist; no scheduled backup or real restore drill/RPO/RTO evidence. |
| `node_protocol_v1` | yes | yes | no | no | no | no | Control-plane endpoints plus client/runner library primitives import and have fake/local tests; no runnable daemon entry point exists. |
| `node_daemon` | no | no | no | no | no | no | `agent/__main__.py` is absent; `python -m agent` and the systemd template cannot start an agent. |
| `node_protocol_v2` | no | no | no | no | no | no | Server-selected lease, current-attempt recovery and canonical Job convergence are future WP-4 work. |
| `execution_attempt_outbox` | yes | yes | no | no | no | no | `DG-EXEC-ATTEMPT-v1` is approved; Phase 1 schema/CAS/guards/stop correction plus WP-2A SQLite leader loop and authenticated read-only attempt/outbox/queue/collection telemetry pass the 2,955-test suite. Remote new-claim/reconcile/outbox workers remain explicitly unimplemented, every flag defaults off, and no production migration/canary exists. |
| `attempt_driven_ssh` | yes | yes | no | no | no | no | WP-2C routes dispatch through `app.execution_dispatch`: DB intent commits before any remote effect, prepare/launch are durable outbox operations, and a launch failure is classified rather than assumed. Gated by `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED`, which defaults off and fails configuration validation without new-claim ownership. No deployment or canary evidence exists; with the flag off the legacy path is unchanged. |
| `immutable_execution_plan` | no | no | no | no | no | no | Ordinary runs do not uniformly pin code, data, profile, target and approval digest; future WP-3 work. |
| `codex_app_server_adapter` | yes | yes | no | no | no | no | Bounded fake protocol adapter exists; production wiring/version pin/canary remain separately gated. |
| `github_publication` | yes | yes | no | no | no | no | Interface and fake only; no production adapter or approved credential/egress configuration. |

## Release blockers carried forward

| Blocker | Current evidence | Required destination |
|---|---|---|
| `RB-DATASET-001` | `POST /datasets` scans a local path and calls `insert_dataset()` directly; `tests/test_api.py::test_create_and_list_dataset` proves the row appears with zero new approvals. No `VALID_APPROVAL_KINDS` entry authorizes this material mutation. | Characterized in WP-0B. Do not extend this path into immutable publishing; replace through `DG-DATASET-SNAPSHOT` / WP-3A. |
| `RB-SERVER-001` | WP-1B dual-read ownership protection is now wired for delete/repoint/rotation, and unresolved journals block all legacy HTTP mutations. The remaining gap is that existing public server approvals still write legacy YAML directly and do not materialize a pinned immutable revision/journal; such legacy-observed targets remain ineligible for generic claims. | `DG-EXEC-ATTEMPT-v1.1` addendum approved 2026-07-27, authorizing the pinned publication rework and one operator recovery surface. Not yet implemented; keep `NEW_CLAIMS=false` until it is. |
| `RB-LAUNCH-001` | **Fixed in code, not yet proven in operation.** WP-2C's attempt path keeps the Job `running` with `liveness=unknown` when a launch response is lost; `test_response_lost_after_tmux_keeps_the_job_running` asserts exactly the scenario that previously requeued a live workload. The legacy branch still reverts unconditionally and remains the default, because the flag is off. | WP-2D canary on a non-production worker (≥20 jobs / 24h, one forced response-loss, one restart), then flag enablement. The blocker closes when the canary passes, not when the code merges. |
| `RB-NODE-001` | V1 protocol primitives exist, but no runnable daemon and no server-selected/recovery-capable v2 contract exist. | `DG-NODE-V2` and WP-4A/4B; keep production workers on SSH. |

## Resolved blockers

| Blocker | Resolution evidence |
|---|---|
| `RB-STOP-001` | WP-1C pins new stop approvals, persists an immutable legacy intent before delivery, never projects kill success/failure to terminal, blocks requeue/re-dispatch while unresolved, and closes only with terminal evidence. `tests/test_approvals.py::test_stop_kill_failure_stays_running_with_uncertain_intent` plus scheduler crash/requeue/terminal tests pass in the 2,951-test full suite. |

## Updating this ledger

Every work package must update its affected rows and blockers. A field moves to
`yes` only with direct evidence recorded in `docs/IMPLEMENTATION_PROGRESS.md`.
Missing deployment/canary evidence stays `unknown` or `no`; it is never inferred
from passing unit tests.
