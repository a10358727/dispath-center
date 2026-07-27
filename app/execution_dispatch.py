"""WP-2C attempt-driven SSH lifecycle (DG-AMBIGUOUS-LAUNCH-v1 §7.2/§7.3).

This is the package that resolves `RB-LAUNCH-001`. WP-2B built the arbitration
primitives; here they finally carry real dispatch, and the unconditional
`running -> queued` revert stops being the only behavior available.

The ordering is fixed and is the whole point:

1. one DB transaction creates the attempt, pins server/backend/revision and
   moves the Job `queued -> running`. No mkdir, SFTP or SSH happens before that
   commit (`INV-STATE-2`).
2. `prepare` and `launch` are durable outbox operations. Each records
   `effect_started_at` *before* its material call, so a crash can always tell
   "never started" from "may have started".
3. a launch failure is classified, never assumed. Only a definite pre-launch
   failure requeues; everything else leaves the Job running with
   `liveness=unknown` and is resolved later by arbitration or evidence.

Everything here is gated by `EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED`, which
defaults to false. With the flag off the legacy scheduler path runs unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.execution_launch import (
    build_attempt_abandon_command,
    build_attempt_inspect_command,
    build_attempt_launch_command,
    build_attempt_launch_sh_content,
    build_attempt_paths,
    build_attempt_prepare_command,
    build_attempt_run_sh_content,
    classify_arbitration_result,
    classify_launch_failure,
    classify_prepare_failure,
    parse_inspect_output,
    resolve_attempt_observation,
    unreachable_resolution,
)

logger = logging.getLogger(__name__)

PREPARE_TIMEOUT_SEC = 30
LAUNCH_TIMEOUT_SEC = 30
INSPECT_TIMEOUT_SEC = 15
OPERATION_CLAIM_SECONDS = 60


@dataclass(frozen=True)
class AttemptLaunchContext:
    """What the scheduler needs to route one server through the attempt path.

    `owns()` is deliberately conservative: a server is routed here only when
    the launch flag is on, this process currently holds the leader lease, and
    the target has an approved, preflight-eligible pinned revision. Anything
    unresolved keeps the server on the legacy SSH path rather than guessing.
    """

    leader_owner_id: str
    scheduler_fencing_epoch: Optional[int]
    enabled: bool = False
    revision_ids: dict[str, str] = None  # type: ignore[assignment]

    def owns(self, server_name: str) -> bool:
        return bool(
            self.enabled
            and self.scheduler_fencing_epoch is not None
            and self.revision_ids
            and server_name in self.revision_ids
        )

    async def dispatch(
        self, db, ssh_run, ssh_write_file, job, server_name: str
    ) -> "DispatchOutcome":
        """The scheduler has already chosen the target, so the revision is
        looked up by that exact server rather than re-derived from the Job."""
        return await dispatch_job_via_attempt(
            db,
            ssh_run,
            ssh_write_file,
            job=job,
            server_config_revision_id=self.revision_ids[server_name],
            leader_owner_id=self.leader_owner_id,
            scheduler_fencing_epoch=self.scheduler_fencing_epoch,
        )


@dataclass(frozen=True)
class DispatchOutcome:
    """`requeued` is only ever true when non-launch was *proven*."""

    attempt_id: Optional[str]
    state: str  # 'running' | 'unknown' | 'requeued' | 'not_attempted'
    reason_code: str
    requeued: bool = False


def _sanitize(exc: BaseException) -> str:
    """Error text may quote remote output, so keep only the type name."""
    return type(exc).__name__


async def dispatch_job_via_attempt(
    db,
    ssh_run: Callable,
    ssh_write_file: Callable,
    *,
    job,
    server_config_revision_id: str,
    leader_owner_id: str,
    scheduler_fencing_epoch: int,
) -> DispatchOutcome:
    """Dispatch one Job through a durable attempt.

    Returns without raising: every failure path is classified and persisted,
    because an exception escaping here would leave the caller guessing exactly
    where the ambiguity this package exists to remove reappears.
    """

    try:
        attempt = db.create_execution_attempt(
            job_id=job.id,
            backend="ssh",
            server_config_revision_id=server_config_revision_id,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
    except ValueError as exc:
        # Nothing was written and nothing remote happened; the Job keeps its
        # current status and the next tick may retry with fresh eligibility.
        return DispatchOutcome(
            attempt_id=None,
            state="not_attempted",
            reason_code=str(exc) or "claim_conflict",
        )

    attempt_id = attempt["id"]
    fencing_token = attempt["fencing_token"]
    server_name = attempt["server_name"]
    paths = build_attempt_paths(job.id, attempt_id)

    prepare_op = db.insert_execution_operation(
        attempt_id=attempt_id,
        operation="prepare",
        payload={
            "command": build_attempt_prepare_command(job.id, attempt_id),
            "attempt_dir": paths["dir"],
        },
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        authorization_approval_id=attempt["execution_approval_id"],
        authorized_contract_sha256=attempt["approved_payload_sha256"],
        idempotency_key=f"prepare:{attempt_id}:{fencing_token}",
    )

    launch_effect_started = False
    try:
        db.claim_execution_operation(
            operation_id=prepare_op["id"],
            claim_owner=leader_owner_id,
            claim_seconds=OPERATION_CLAIM_SECONDS,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
        db.mark_execution_operation_effect_started(
            operation_id=prepare_op["id"],
            claim_owner=leader_owner_id,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
        await ssh_run(
            server_name,
            build_attempt_prepare_command(job.id, attempt_id),
            PREPARE_TIMEOUT_SEC,
        )
        await ssh_write_file(server_name, paths["cmd_sh"], job.command)
        await ssh_write_file(
            server_name, paths["run_sh"], build_attempt_run_sh_content(job.id, attempt_id)
        )
        await ssh_write_file(
            server_name,
            paths["launch_sh"],
            build_attempt_launch_sh_content(job.id, attempt_id, fencing_token),
        )
        db.transition_execution_operation(
            operation_id=prepare_op["id"],
            expected_state="processing",
            new_state="delivered",
            reason_code="contract_validated",
            evidence={"attempt_dir": paths["dir"]},
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
            claim_owner=leader_owner_id,
        )
    except Exception as exc:  # noqa: BLE001
        verdict = classify_prepare_failure(launch_effect_started=launch_effect_started)
        return _settle_failed_launch(
            db,
            attempt=attempt,
            operation_id=prepare_op["id"],
            verdict=verdict,
            exc=exc,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )

    launch_op = db.insert_execution_operation(
        attempt_id=attempt_id,
        operation="launch",
        payload={
            "command": build_attempt_launch_command(job.id, attempt_id),
            "session": paths["dir"],
        },
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        authorization_approval_id=attempt["execution_approval_id"],
        authorized_contract_sha256=attempt["approved_payload_sha256"],
        # Replay reuses this exact key against this exact target. A second
        # launch operation for the same attempt is a bug, not a retry.
        idempotency_key=f"launch:{attempt_id}:{fencing_token}",
    )

    try:
        db.claim_execution_operation(
            operation_id=launch_op["id"],
            claim_owner=leader_owner_id,
            claim_seconds=OPERATION_CLAIM_SECONDS,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
        db.mark_execution_operation_effect_started(
            operation_id=launch_op["id"],
            claim_owner=leader_owner_id,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
        launch_effect_started = True
        await ssh_run(
            server_name,
            build_attempt_launch_command(job.id, attempt_id),
            LAUNCH_TIMEOUT_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        verdict = classify_launch_failure(exc, effect_started=launch_effect_started)
        return _settle_failed_launch(
            db,
            attempt=attempt,
            operation_id=launch_op["id"],
            verdict=verdict,
            exc=exc,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )

    db.transition_execution_operation(
        operation_id=launch_op["id"],
        expected_state="processing",
        new_state="delivered",
        reason_code="contract_validated",
        evidence={"launch_contract": "attempt-launcher-v2"},
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        claim_owner=leader_owner_id,
    )
    db.transition_execution_attempt(
        attempt_id=attempt_id,
        expected_state="dispatching",
        expected_liveness="known",
        new_state="running",
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        reason_code="remote_state_observed",
        evidence={"launch_acknowledged": True},
    )
    return DispatchOutcome(
        attempt_id=attempt_id, state="running", reason_code="remote_state_observed"
    )


def _settle_failed_launch(
    db,
    *,
    attempt,
    operation_id: str,
    verdict,
    exc: BaseException,
    leader_owner_id: str,
    scheduler_fencing_epoch: int,
) -> DispatchOutcome:
    """Persist a classified failure.

    The definite branch is the only one that may return the Job to `queued`,
    and it is reached only when the classification proved non-launch.
    """

    attempt_id = attempt["id"]
    if verdict.may_requeue:
        db.transition_execution_operation(
            operation_id=operation_id,
            expected_state="processing",
            new_state="failed",
            reason_code="pre_effect_definite_failure",
            evidence={"launch_reason_code": verdict.reason_code},
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
            claim_owner=leader_owner_id,
            last_error_category=verdict.reason_code,
            sanitized_error_detail=_sanitize(exc),
            # The admissible evidence for the definite verdict. The DB guard
            # reads this before it will allow `abandoned_before_launch`.
            transmission_state=verdict.transmission_state,
        )
        db.transition_execution_attempt(
            attempt_id=attempt_id,
            expected_state=attempt["state"],
            expected_liveness="known",
            new_state="abandoned_before_launch",
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
            reason_code="pre_effect_definite_failure",
            evidence={"launch_reason_code": verdict.reason_code},
        )
        db.requeue_job_after_abandoned_attempt(attempt_id=attempt_id)
        return DispatchOutcome(
            attempt_id=attempt_id,
            state="requeued",
            reason_code=verdict.reason_code,
            requeued=True,
        )

    # Ambiguous. The Job stays running on this server, the attempt keeps its
    # target, and the operation is uncertain rather than failed. Nothing here
    # may create a second attempt or pick a different server.
    db.transition_execution_operation(
        operation_id=operation_id,
        expected_state="processing",
        new_state="uncertain",
        reason_code="effect_outcome_unknown",
        evidence={"launch_reason_code": verdict.reason_code},
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        claim_owner=leader_owner_id,
        last_error_category=verdict.reason_code,
        sanitized_error_detail=_sanitize(exc),
        transmission_state=verdict.transmission_state,
    )
    db.transition_execution_attempt(
        attempt_id=attempt_id,
        expected_state=attempt["state"],
        expected_liveness="known",
        new_liveness="unknown",
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        reason_code="effect_outcome_unknown",
        evidence={"launch_reason_code": verdict.reason_code},
    )
    return DispatchOutcome(
        attempt_id=attempt_id, state="unknown", reason_code=verdict.reason_code
    )


async def reconcile_attempt(
    db,
    ssh_run: Callable,
    *,
    attempt: dict[str, Any],
    job_id: int,
    leader_owner_id: str,
    scheduler_fencing_epoch: int,
) -> str:
    """Resolve one non-terminal attempt from remote evidence.

    Reads only. Any state change is derived from `resolve_attempt_observation`,
    so "I saw nothing" can never become a terminal verdict or a requeue.
    """

    attempt_id = attempt["id"]
    try:
        result = await ssh_run(
            attempt["server_name"],
            build_attempt_inspect_command(job_id, attempt_id),
            INSPECT_TIMEOUT_SEC,
        )
    except Exception:  # noqa: BLE001
        resolution = unreachable_resolution()
        _apply_liveness_only(
            db,
            attempt=attempt,
            resolution=resolution,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
        return resolution.reason_code

    observation = parse_inspect_output(getattr(result, "stdout", "") or "")
    resolution = resolve_attempt_observation(
        observation, attempt_id, attempt["fencing_token"]
    )

    if resolution.attempt_state is None:
        _apply_liveness_only(
            db,
            attempt=attempt,
            resolution=resolution,
            leader_owner_id=leader_owner_id,
            scheduler_fencing_epoch=scheduler_fencing_epoch,
        )
        return resolution.reason_code

    reason_code = (
        "terminal_evidence_valid"
        if resolution.attempt_state in {"done", "failed"}
        else "remote_state_observed"
    )
    db.transition_execution_attempt(
        attempt_id=attempt_id,
        expected_state=attempt["state"],
        expected_liveness=attempt["liveness"],
        new_state=resolution.attempt_state,
        new_liveness="known",
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        reason_code=reason_code,
        evidence={"launch_reason_code": resolution.reason_code},
        exit_code=resolution.exit_code,
    )
    if resolution.job_status is not None:
        db.apply_attempt_resolution_to_job(
            attempt_id=attempt_id,
            job_status=resolution.job_status,
            exit_code=resolution.exit_code,
        )
    return resolution.reason_code


def _apply_liveness_only(
    db, *, attempt, resolution, leader_owner_id: str, scheduler_fencing_epoch: int
) -> None:
    """An unresolved observation may move liveness and nothing else."""
    if attempt["liveness"] == resolution.liveness:
        return
    db.transition_execution_attempt(
        attempt_id=attempt["id"],
        expected_state=attempt["state"],
        expected_liveness=attempt["liveness"],
        new_liveness=resolution.liveness,
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        reason_code=(
            "remote_unreachable"
            if resolution.reason_code == "unknown_target_unreachable"
            else "effect_outcome_unknown"
        ),
        evidence={"launch_reason_code": resolution.reason_code},
    )


async def arbitrate_unknown_attempt(
    db,
    ssh_run: Callable,
    *,
    attempt: dict[str, Any],
    job_id: int,
    leader_owner_id: str,
    scheduler_fencing_epoch: int,
) -> DispatchOutcome:
    """Try to reach a definite verdict for an attempt stuck at `unknown`.

    The controller races the launcher for the claim. Winning proves the
    workload never started; losing proves only that someone claimed first, and
    never permits a requeue.
    """

    attempt_id = attempt["id"]
    try:
        result = await ssh_run(
            attempt["server_name"],
            build_attempt_abandon_command(job_id, attempt_id),
            INSPECT_TIMEOUT_SEC,
        )
    except Exception:  # noqa: BLE001
        return DispatchOutcome(
            attempt_id=attempt_id,
            state="unknown",
            reason_code="unknown_target_unreachable",
        )

    verdict = classify_arbitration_result(getattr(result, "stdout", "") or "")
    if not verdict.may_requeue:
        return DispatchOutcome(
            attempt_id=attempt_id, state="unknown", reason_code=verdict.reason_code
        )

    # Winning the claim is itself a remote observation, and it is the evidence
    # the abandon guard checks for before permitting a requeue.
    db.record_launch_not_transmitted(attempt_id=attempt_id)
    db.transition_execution_attempt(
        attempt_id=attempt_id,
        expected_state=attempt["state"],
        expected_liveness=attempt["liveness"],
        new_state="abandoned_before_launch",
        new_liveness="known",
        leader_owner_id=leader_owner_id,
        scheduler_fencing_epoch=scheduler_fencing_epoch,
        reason_code="pre_effect_definite_failure",
        evidence={"launch_reason_code": verdict.reason_code},
    )
    db.requeue_job_after_abandoned_attempt(attempt_id=attempt_id)
    return DispatchOutcome(
        attempt_id=attempt_id,
        state="requeued",
        reason_code=verdict.reason_code,
        requeued=True,
    )
