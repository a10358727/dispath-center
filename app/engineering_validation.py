"""Fail-closed contract checks for ordinary Jobs owned by worker validation.

These Jobs intentionally keep ``engineering_task_id`` NULL so the existing
scheduler treats them as ordinary worker work.  The dedicated validation
back-reference is therefore the authority for command/target immutability.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Mapping, Optional

from app.activity import ProjectInstanceResolutionError, resolve_project_instance
from app.config import ServerConfig
from app.datasets import LOCAL_SERVER
from app.db import Database, Job
from app.engineering_tasks import inspect_engineering_result_file
from app.results import local_result_dir


ENGINEERING_VALIDATION_CONTRACT_VERSION = "engineering-worker-validation-v1"

logger = logging.getLogger(__name__)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot_digest(snapshot: dict) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return _digest(encoded)


def _target_identity(server: ServerConfig) -> dict:
    return {
        "name": server.name,
        "host": server.host,
        "user": server.user,
        "port": server.port,
    }


def _instance_identity(instance) -> dict:
    return {
        "id": instance.id,
        "project_id": instance.project_id,
        "server": instance.server,
        "path": instance.path,
    }


def _expected_approval_payload(validation, snapshot: dict) -> Optional[dict]:
    task = snapshot.get("task")
    target = snapshot.get("target")
    instance = snapshot.get("project_instance")
    bundle = snapshot.get("bundle")
    job = snapshot.get("job")
    parent_approval = snapshot.get("parent_approval")
    execution = snapshot.get("execution")
    if not all(
        isinstance(value, dict)
        for value in (
            task,
            target,
            instance,
            bundle,
            job,
            parent_approval,
            execution,
        )
    ):
        return None
    return {
        "command": job.get("command"),
        "type": job.get("type"),
        "project": task.get("project_name"),
        "require_tag": job.get("require_tag"),
        "pin_server": target.get("name"),
        "depends_on": [],
        "gpus_needed": job.get("gpus_needed"),
        "priority": job.get("priority"),
        "sync_plan": None,
        "setup_plan": None,
        "warning": None,
        "source": "engineering_validation",
        "source_coding_run_id": task.get("coding_run_id"),
        "validation_request_id": validation.id,
        "validation_contract_version": ENGINEERING_VALIDATION_CONTRACT_VERSION,
        "engineering_task_id": task.get("id"),
        "attempt_number": task.get("attempt_number"),
        "project_id": task.get("project_id"),
        "project_version_id": task.get("project_version_id"),
        "base_commit": task.get("base_commit"),
        "result_commit": task.get("result_commit"),
        "request_snapshot_sha256": validation.request_snapshot_sha256,
        "validation_execution_summary": {
            "contract_version": ENGINEERING_VALIDATION_CONTRACT_VERSION,
            "parent_approval": parent_approval,
            "target_identity": target,
            "project_instance": instance,
            "bundle": bundle,
            "command_digests": execution,
        },
    }


def _engineering_validation_job_contract_failure_unchecked(
    db: Database,
    job: Job,
    server_configs: Mapping[str, ServerConfig],
    *,
    local_home_dir: Optional[str] = None,
) -> Optional[str]:
    """Return a fixed refusal category, or ``None`` for valid/unlinked Jobs.

    Callers must first test ``job.engineering_validation_request_id`` to
    distinguish an unrelated legacy Job from a linked and valid validation
    Job.  Categories deliberately contain no command, host, path, or error
    text and are safe for audit/status evidence.
    """

    request_id = job.engineering_validation_request_id
    if request_id is None:
        return None
    validation = db.get_engineering_validation_request_by_job_id(job.id)
    if validation is None or validation.id != request_id:
        return "validation_linkage_mismatch"
    snapshot = validation.request_snapshot
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("contract_version")
        != ENGINEERING_VALIDATION_CONTRACT_VERSION
        or _snapshot_digest(snapshot) != validation.request_snapshot_sha256
    ):
        return "validation_snapshot_mismatch"
    task_snapshot = snapshot.get("task")
    target_snapshot = snapshot.get("target")
    instance_snapshot = snapshot.get("project_instance")
    bundle_snapshot = snapshot.get("bundle")
    job_snapshot = snapshot.get("job")
    parent_approval_snapshot = snapshot.get("parent_approval")
    execution_snapshot = snapshot.get("execution")
    if not all(
        isinstance(value, dict)
        for value in (
            task_snapshot,
            target_snapshot,
            instance_snapshot,
            bundle_snapshot,
            job_snapshot,
            parent_approval_snapshot,
            execution_snapshot,
        )
    ):
        return "validation_snapshot_mismatch"

    approval = db.get_approval(validation.approval_id)
    expected_approval_payload = _expected_approval_payload(validation, snapshot)
    if (
        approval is None
        or approval.kind != "enqueue"
        or approval.status != "approved"
        or expected_approval_payload is None
        or approval.payload != expected_approval_payload
    ):
        return "validation_approval_mismatch"
    task = db.get_engineering_task(validation.engineering_task_id)
    run = db.get_coding_run(validation.coding_run_id)
    version = db.get_project_version(validation.project_version_id)
    if task is None or run is None or version is None:
        return "validation_parent_mismatch"
    parent_approval = db.get_approval(task.approval_id)
    if (
        parent_approval is None
        or parent_approval.kind != "coding_task"
        or parent_approval.status != "approved"
        or parent_approval.id != parent_approval_snapshot.get("id")
        or _snapshot_digest(parent_approval.payload)
        != parent_approval_snapshot.get("payload_sha256")
        or parent_approval.payload.get("engineering_task_id") != task.id
        or parent_approval.payload.get("project_id") != task.project_id
        or parent_approval.payload.get("project_version_id")
        != task.project_version_id
        or parent_approval.payload.get("base_commit") != task.base_commit
        or task.id != validation.engineering_task_id
        or task.project_id != validation.project_id
        or task.project_name != validation.project_name
        or task.project_version_id != validation.project_version_id
        or task.base_commit != validation.base_commit
        or task.coding_run_id != run.id
        or run.engineering_task_id != task.id
        or run.attempt_number != validation.attempt_number
        or run.project_version_id != validation.project_version_id
        or run.base_binding != "project_version_pinned"
        or run.base_commit != validation.base_commit
        or run.status != "done"
        or run.result_commit != validation.result_commit
        or version.project_id != validation.project_id
        or version.project_name != validation.project_name
        or version.git_commit != validation.base_commit
    ):
        return "validation_parent_mismatch"

    target = server_configs.get(validation.target_server)
    if (
        target is None
        or not target.enabled
        or target.name == LOCAL_SERVER
        or _target_identity(target) != target_snapshot
    ):
        return "validation_target_identity_mismatch"
    try:
        instance = resolve_project_instance(
            db, validation.project_name, validation.target_server
        )
    except ProjectInstanceResolutionError:
        return "validation_instance_identity_mismatch"
    if _instance_identity(instance) != instance_snapshot:
        return "validation_instance_identity_mismatch"

    if local_home_dir is None or run.job_id is None:
        return "validation_bundle_unavailable"
    inspected = inspect_engineering_result_file(
        result_dir=local_result_dir(run.job_id, local_home_dir),
        filename="changes.bundle",
    )
    current_bundle = {
        "storage_key": inspected.get("storage_key"),
        "sha256": inspected.get("sha256"),
        "size_bytes": inspected.get("size_bytes"),
    }
    if not inspected.get("available") or current_bundle != bundle_snapshot:
        return "validation_bundle_mismatch"
    artifact = next(
        (
            item
            for item in db.list_engineering_task_artifacts(
                task.id, attempt_number=validation.attempt_number
            )
            if item.kind == "bundle"
            and item.coding_run_id == run.id
            and item.verification_status == "verified"
            and item.availability == "available"
        ),
        None,
    )
    if (
        artifact is None
        or artifact.source_sha256 != bundle_snapshot.get("sha256")
        or artifact.source_size_bytes != bundle_snapshot.get("size_bytes")
    ):
        return "validation_bundle_mismatch"

    command_digest = _digest(job.command)
    if (
        validation.bundle_push_command_sha256
        != execution_snapshot.get("bundle_push_command_sha256")
        or validation.downstream_command_sha256
        != execution_snapshot.get("downstream_command_sha256")
    ):
        return "validation_job_contract_mismatch"
    common_job_mismatch = (
        job.project != validation.project_name
        or job.engineering_task_id is not None
        or job.engineering_task_role is not None
        or job.engineering_attempt_number is not None
    )
    if job.id == validation.bundle_push_job_id:
        if (
            common_job_mismatch
            or validation.bundle_push_command_sha256 != command_digest
            or job.type != "sync"
            or job.pin_server != LOCAL_SERVER
            or job.depends_on
            or job.source_coding_run_id is not None
            or (job.server is not None and job.server != LOCAL_SERVER)
        ):
            return "validation_job_contract_mismatch"
    elif job.id == validation.downstream_job_id:
        if (
            common_job_mismatch
            or validation.downstream_command_sha256 != command_digest
            or job.type != job_snapshot.get("type")
            or job.pin_server != validation.target_server
            or job.depends_on != [validation.bundle_push_job_id]
            or job.source_coding_run_id != validation.coding_run_id
            or job.require_tag != job_snapshot.get("require_tag")
            or job.gpus_needed != job_snapshot.get("gpus_needed")
            or job.priority != job_snapshot.get("priority")
            or (job.server is not None and job.server != validation.target_server)
        ):
            return "validation_job_contract_mismatch"
    else:
        return "validation_linkage_mismatch"
    return None


def engineering_validation_job_contract_failure(
    db: Database,
    job: Job,
    server_configs: Mapping[str, ServerConfig],
    *,
    local_home_dir: Optional[str] = None,
) -> Optional[str]:
    """Fail closed with a fixed category when durable validation data is corrupt.

    The unchecked verifier intentionally performs several DB decodes, filesystem
    inspections and project-instance lookups.  None of their exception text is
    safe to surface at an execution boundary, and a presentation/schema problem
    must never make a linked Job fall back to legacy execution semantics.
    """

    if job.engineering_validation_request_id is None:
        return None
    try:
        return _engineering_validation_job_contract_failure_unchecked(
            db,
            job,
            server_configs,
            local_home_dir=local_home_dir,
        )
    except Exception:  # noqa: BLE001 - immutable execution boundary is fail closed
        return "validation_contract_unavailable"


def engineering_validation_job_audit_fields(job: Job) -> dict[str, str]:
    """Return the only audit-safe description of a validation executor command."""

    label = (
        "Push verified Engineering Task bundle to approved worker"
        if job.type == "sync"
        else "Run approved Engineering Task worker validation"
    )
    return {
        "command_display": label,
        "command_digest": _digest(job.command),
        "command_digest_algorithm": "sha256",
        "validation_request_id": job.engineering_validation_request_id or "unknown",
    }


def record_engineering_validation_contract_refusal(
    db: Database,
    job: Job,
    failure_category: str,
) -> None:
    """Best-effort fixed evidence for a linked Job that was not allowed contact.

    Failure categories are produced internally by this module and contain no
    command, path, host or exception detail.  The visibility journal is never
    allowed to weaken or crash the already fail-closed execution decision.
    """

    request_id = job.engineering_validation_request_id
    if request_id is None:
        return
    try:
        validation = db.get_engineering_validation_request_by_job_id(job.id)
        if validation is None or validation.id != request_id:
            return
        db.append_engineering_task_event(
            task_id=validation.engineering_task_id,
            attempt_number=validation.attempt_number,
            event_key=f"worker-validation:{request_id}:job:{job.id}:contract-refusal",
            event_type="worker_validation_contract_refusal",
            phase="validation",
            state="disconnected",
            source_kind="system",
            source_id=str(job.id),
            summary=(
                "Worker validation execution contract mismatch; executor not contacted"
            ),
            details={
                "job_id": job.id,
                "validation_request_id": request_id,
                "failure_category": failure_category,
            },
        )
    except Exception:  # noqa: BLE001 - journal failure cannot reopen execution
        logger.warning(
            "Worker validation Job #%s refusal event could not be recorded; "
            "execution remains blocked",
            job.id,
        )
