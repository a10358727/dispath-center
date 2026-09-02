"""Engineering Task / Coding Run presentation and detail-building layer.

Extracted verbatim from `app/main.py` (DG-UI-UNIFICATION v1 U6a) so the new
`/api/v2/engineering-tasks*`/`/api/v2/coding-runs*` thin wrapper router
(`dispatch_center/api/routers/engineering_v2.py`) can reuse the exact same
security-sensitive redaction/presentation pipeline as the legacy
`/engineering-tasks*`/`/coding-runs*` surface -- a single source of truth
rather than a second hand-maintained copy of this redaction logic (unlike the
smaller, purely mechanical projections duplicated in `jobs_v2.py`/
`projects_legacy_v2.py`, this module governs what secrets/paths/commands are
safe to reveal, so a drifted duplicate would be a real leak risk). This
mirrors the `app/job_projection.py` precedent: `app/main.py`'s own private
`_foo` names now delegate to the public functions here (see each delegator's
one-line body in `app/main.py`), so every existing call site in `app/main.py`
is unchanged.

Every function that needs the running `AppState` takes it as an explicit
first parameter (`app_state: Any`, duck-typed exactly like
`jobs_v2._runtime()`/`approvals_module.approve()`) instead of closing over
the module-level global the way the original private functions in
`app/main.py` did -- the only behavioral change from extraction, and it does
not alter any external return value or control flow.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Optional

from app.approvals import CODING_RUN_TERMINAL_STATUSES, resolve_codex_workspace_rel
from app.code_promotion import PromotionCandidateError, resolve_promotion_candidate
from app.db import (
    Approval,
    CodingRun,
    EngineeringTask,
    EngineeringTaskArtifact,
    EngineeringTaskCommand,
    EngineeringTaskEvent,
    EngineeringValidationRequest,
    Job,
    VALID_STATUSES,
)
from app.engineering_path_policy import (
    EngineeringPathPolicyError,
    validate_engineering_path_policy,
    validate_engineering_path_verifier_contract,
)
from app.engineering_tasks import (
    ENGINEERING_TASK_SOURCE_FILE_LIMIT,
    capture_sanitized_engineering_patch,
    inspect_engineering_result_file,
    redact_engineering_text,
    remote_engineering_bundle_path,
)
from app.engineering_validation import engineering_validation_job_contract_failure
from app.results import local_result_dir

#: The Engineering Task execution backend was retired (DG-AGENT-RUNTIME-V3
#: Phase 1b; surfaces deleted in DG-CONSOLIDATION-v1 C-5 (c)). The read-only
#: history keeps reporting its actions as unavailable.
_ENGINEERING_TASK_BACKEND_ENABLED = False


def safe_engineering_status(value: Any, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def safe_engineering_exit_code(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 255 else None


def safe_engineering_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return value


def safe_engineering_visibility_value(value: Any) -> Any:
    """Recursively redact user/provider metadata before visibility responses."""

    if isinstance(value, str):
        preview = redact_engineering_text(value, max_chars=4096)
        return None if preview.get("withheld") else preview.get("content")
    if isinstance(value, list):
        return [safe_engineering_visibility_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): safe_engineering_visibility_value(item)
            for key, item in value.items()
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return None


SAFE_ENGINEERING_TASK_STATUSES = {
    "pending_approval",
    "planning",
    "rejected",
    "queued",
    "staging",
    "staging_failed",
    "running",
    "finalizing",
    "done",
    "no_changes",
    "failed",
    "secret_violation",
    "path_policy_violation",
    "blocked",
    "cancelled",
    "interrupted",
    "disconnected",
    "discarded",
    "unknown",
}
SAFE_ENGINEERING_CODING_RUN_STATUSES = {
    "queued",
    "running",
    "done",
    "no_changes",
    "failed",
    "secret_violation",
    "path_policy_violation",
    "cancelled",
    "unknown",
}


def engineering_execution_contract_projection(task: EngineeringTask) -> dict:
    """Expose policy facts, never Runner connection or managed path details."""

    contract = task.execution_contract
    runner = contract.get("runner") if isinstance(contract, dict) else None
    runner_name = runner.get("name") if isinstance(runner, dict) else task.runner_server
    final_path_policy = task.contract_version == "engineering-task-v2"
    policy_digest = contract.get("path_policy_sha256")
    if not isinstance(policy_digest, str) or re.fullmatch(
        r"[0-9a-f]{64}", policy_digest
    ) is None:
        policy_digest = None
    return {
        "runner": {"name": runner_name},
        "workspace": "managed_isolated_worktree",
        "source_kind": contract.get("source_kind"),
        "immutable_base": True,
        "network_access": contract.get("network_access") is True,
        "dependency_installation": contract.get("dependency_installation") is True,
        "path_policy": {
            "enforcement": (
                "runner_pre_bundle_and_server_a_pre_accept"
                if final_path_policy
                else "advisory"
            ),
            "scope": "final_git_diff" if final_path_policy else None,
            "turn_time_filesystem_confinement": False,
            "policy_sha256": policy_digest if final_path_policy else None,
        },
        "details_withheld": True,
    }


def engineering_task_to_dict(task: EngineeringTask) -> dict:
    instruction_preview = redact_engineering_text(task.instruction, max_chars=4096)
    return {
        "id": task.id,
        "record_kind": "engineering_task",
        "legacy": False,
        "approval_id": task.approval_id,
        "coding_run_id": task.coding_run_id,
        "project_id": task.project_id,
        "project": task.project_name,
        "project_version_id": task.project_version_id,
        "base_commit": task.base_commit,
        "base_binding": "project_version_pinned",
        "agent_provider_id": task.agent_provider_id,
        "provider_capabilities": safe_engineering_visibility_value(
            task.provider_capabilities
        ),
        "execution_contract": engineering_execution_contract_projection(task),
        "contract_version": task.contract_version,
        "structured_request": safe_engineering_visibility_value(
            task.structured_request
        ),
        "instruction": (
            None
            if instruction_preview.get("withheld")
            else instruction_preview.get("content")
        ),
        "instruction_visibility": {
            "redacted": bool(instruction_preview.get("redacted")),
            "withheld": bool(instruction_preview.get("withheld")),
            "truncated": bool(instruction_preview.get("truncated")),
        },
        "detected_metadata": safe_engineering_visibility_value(
            task.detected_metadata
        ),
        "runner_server": task.runner_server,
        "validation_target": task.validation_target,
        "status": safe_engineering_status(
            task.status, SAFE_ENGINEERING_TASK_STATUSES
        ),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def legacy_coding_run_to_engineering_task(run: CodingRun) -> dict:
    """誠實呈現舊 CodingRun，不為歷史資料猜 ProjectVersion。"""

    instruction_preview = redact_engineering_text(run.instruction, max_chars=4096)
    return {
        "id": f"legacy-coding-run-{run.id}",
        "record_kind": "legacy_coding_run",
        "legacy": True,
        "approval_id": run.approval_id,
        "coding_run_id": run.id,
        "project_id": None,
        "project": run.project,
        "project_version_id": None,
        "base_commit": None,
        "observed_base_commit": run.base_commit,
        "base_binding": "legacy_unpinned",
        "agent_provider_id": "codex",
        "contract_version": None,
        "instruction": (
            None
            if instruction_preview.get("withheld")
            else instruction_preview.get("content")
        ),
        "instruction_visibility": {
            "redacted": bool(instruction_preview.get("redacted")),
            "withheld": bool(instruction_preview.get("withheld")),
            "truncated": bool(instruction_preview.get("truncated")),
        },
        "runner_server": run.runner_server,
        "validation_target": run.validation_target,
        "status": safe_engineering_status(
            run.status, SAFE_ENGINEERING_CODING_RUN_STATUSES
        ),
        "created_at": run.created_at,
        "updated_at": run.finished_at or run.started_at or run.created_at,
    }


ENGINEERING_STATE_LABELS = {
    "pending_approval": "等待核准",
    "rejected": "已拒絕",
    "queued": "排隊中",
    "staging": "準備基準",
    "staging_failed": "基準準備失敗",
    "running": "執行中",
    "finalizing": "收集結果",
    "done": "完成",
    "no_changes": "無變更",
    "failed": "失敗",
    "secret_violation": "安全檢查拒絕",
    "path_policy_violation": "路徑政策拒絕",
    "blocked": "受阻",
    "cancelled": "已取消",
    "discarded": "已作廢",
    "unknown": "狀態未知",
}
ENGINEERING_PHASE_LABELS = {
    "approval": "核准",
    "queue": "佇列",
    "staging": "基準準備",
    "execution": "代理執行",
    "result_collection": "結果收集",
    "complete": "完成",
    "unknown": "未知",
}

SAFE_ENGINEERING_EVENT_STATES = SAFE_ENGINEERING_TASK_STATUSES | {
    "requested",
}
SAFE_ENGINEERING_EVENT_PHASES = {
    "approval",
    "queue",
    "staging",
    "execution",
    "validation",
    "finalization",
    "result_collection",
    "complete",
    "unknown",
}
ENGINEERING_EVENT_STATUS_DETAIL_KEYS = {
    "previous_state",
    "previous_status",
    "state",
    "status",
}


def safe_engineering_event_details(value: Any) -> Any:
    """Keep journal metadata useful without reflecting unknown state strings."""

    projected = safe_engineering_visibility_value(value)
    if not isinstance(projected, dict):
        return projected
    safe: dict[str, Any] = {}
    for key, item in projected.items():
        if key in ENGINEERING_EVENT_STATUS_DETAIL_KEYS and isinstance(item, str):
            safe[key] = safe_engineering_status(item, SAFE_ENGINEERING_EVENT_STATES)
        elif isinstance(item, dict):
            safe[key] = safe_engineering_event_details(item)
        elif isinstance(item, list):
            safe[key] = [
                safe_engineering_event_details(entry)
                if isinstance(entry, dict)
                else entry
                for entry in item
            ]
        else:
            safe[key] = item
    return safe


def engineering_event_to_dict(event: EngineeringTaskEvent) -> dict:
    safe_state = safe_engineering_status(event.state, SAFE_ENGINEERING_EVENT_STATES)
    details = safe_engineering_event_details(event.details)
    status_detail_unrecognized = bool(
        isinstance(details, dict)
        and any(
            key in details
            and details[key] == "unknown"
            and isinstance(event.details.get(key), str)
            and event.details.get(key) != "unknown"
            for key in ENGINEERING_EVENT_STATUS_DETAIL_KEYS
        )
    )
    unrecognized_state = safe_state == "unknown" and event.state != "unknown"
    event_key = event.event_key
    event_type = event.event_type
    summary = safe_engineering_visibility_value(event.summary)
    if unrecognized_state:
        event_key = f"event:{event.id}:unrecognized-state"
        event_type = "job_status_unrecognized"
        summary = "工作回報了無法識別的狀態"
    elif status_detail_unrecognized and event_type in {
        "job_started",
        "job_finished",
        "job_status_changed",
        "status_changed",
    }:
        event_key = f"event:{event.id}:{event_type}"
    if not isinstance(summary, str) or not summary:
        summary = "事件詳細內容已隱藏"
    return {
        "id": event.id,
        "event_key": event_key,
        "attempt_number": event.attempt_number,
        "type": event_type,
        "phase": safe_engineering_status(event.phase, SAFE_ENGINEERING_EVENT_PHASES),
        "state": safe_state,
        "summary": summary,
        "details": details,
        "source": {"kind": event.source_kind, "id": event.source_id},
        "actor_id": event.actor_id,
        "occurred_at": safe_engineering_timestamp(event.occurred_at),
        "recorded_at": safe_engineering_timestamp(event.recorded_at),
        "origin": "journal",
    }


def legacy_engineering_events(run: CodingRun) -> list[dict]:
    events = [
        {
            "id": None,
            "event_key": f"legacy:{run.id}:created",
            "attempt_number": None,
            "type": "legacy_run_created",
            "phase": "queue",
            "state": "queued",
            "summary": "Legacy Coding Run 已建立",
            "details": {"coding_run_id": run.id},
            "source": {"kind": "coding_run", "id": str(run.id)},
            "actor_id": None,
            "occurred_at": safe_engineering_timestamp(run.created_at),
            "recorded_at": None,
            "origin": "legacy_snapshot",
        }
    ]
    if run.started_at:
        events.append(
            {
                **events[0],
                "event_key": f"legacy:{run.id}:started",
                "type": "legacy_run_started",
                "phase": "execution",
                "state": "running",
                "summary": "Legacy Coding Run 已開始",
                "occurred_at": safe_engineering_timestamp(run.started_at),
            }
        )
    if run.finished_at:
        safe_status = safe_engineering_status(
            run.status, SAFE_ENGINEERING_CODING_RUN_STATUSES
        )
        events.append(
            {
                **events[0],
                "event_key": f"legacy:{run.id}:finished",
                "type": "legacy_run_finished",
                "phase": "complete",
                "state": safe_status,
                "summary": (
                    f"Legacy Coding Run 結束（{safe_status}）"
                    if safe_status != "unknown"
                    else "Legacy Coding Run 已結束，結果狀態未知"
                ),
                "occurred_at": safe_engineering_timestamp(run.finished_at),
            }
        )
    return events


def engineering_command_to_dict(app_state: Any, command: EngineeringTaskCommand) -> dict:
    job = app_state.db.get_job(command.job_id) if command.job_id is not None else None
    status = safe_engineering_status(
        job.status if job else command.recorded_status,
        VALID_STATUSES | {"unknown"},
    )
    raw_started_at = job.started_at if job else command.recorded_started_at
    raw_finished_at = job.finished_at if job else command.recorded_finished_at
    started_at = (
        safe_engineering_timestamp(raw_started_at)
        if status in {"running", "done", "failed", "blocked", "cancelled"}
        else None
    )
    finished_at = (
        safe_engineering_timestamp(raw_finished_at)
        if status in {"done", "failed", "blocked", "cancelled"}
        else None
    )
    duration_seconds = None
    if started_at and finished_at:
        duration_seconds = max(
            0,
            int(
                (
                    datetime.fromisoformat(finished_at)
                    - datetime.fromisoformat(started_at)
                ).total_seconds()
            ),
        )
    execution_location_label = {
        "server_a": "Server A",
        "coding_runner": "Coding Runner",
        "worker": "Worker Job",
    }.get(command.execution_location)
    working_directory_label = safe_engineering_visibility_value(
        command.working_directory_label
    )
    if not isinstance(working_directory_label, str) or not working_directory_label:
        working_directory_label = None
    terminal = status in {"done", "failed", "cancelled"}
    return {
        "id": command.id,
        "attempt_number": command.attempt_number,
        "sequence": command.sequence,
        "role": command.command_role,
        "display_command": command.display_command,
        "command_digest": command.command_digest,
        "execution_location": command.execution_location,
        "execution_location_label": execution_location_label,
        "target_ref": command.target_ref,
        "working_directory": working_directory_label,
        "working_directory_label": working_directory_label,
        "status": status,
        "status_source": (
            "job"
            if job
            else command.status_source
            if command.status_source in {"job", "coding_run", "recorded"}
            else "unknown"
        ),
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": (
            safe_engineering_exit_code(
                job.exit_code if job else command.recorded_exit_code
            )
            if terminal
            else None
        ),
        "duration_seconds": duration_seconds,
        "policy_family": command.policy_family,
        "policy_disposition": command.policy_disposition,
        "approval": {
            "required": command.policy_disposition == "separate_approval_required",
            "approval_id": command.approval_id,
        },
        "log": {
            "available": bool(job and job.log_tail),
            "url": (
                f"/api/v2/engineering-tasks/{command.engineering_task_id}/commands/{command.id}/log"
                if job is not None
                else None
            ),
        },
    }


def legacy_engineering_command(app_state: Any, run: CodingRun) -> list[dict]:
    job = app_state.db.get_job(run.job_id) if run.job_id is not None else None
    if job is None:
        return []
    status = safe_engineering_status(job.status, VALID_STATUSES | {"unknown"})
    started_at = (
        safe_engineering_timestamp(job.started_at)
        if status in {"running", "done", "failed", "blocked", "cancelled"}
        else None
    )
    finished_at = (
        safe_engineering_timestamp(job.finished_at)
        if status in {"done", "failed", "blocked", "cancelled"}
        else None
    )
    return [
        {
            "id": f"legacy-job-{job.id}",
            "attempt_number": None,
            "sequence": 1,
            "role": "agent_turn",
            "display_command": "Legacy Codex agent turn (command withheld)",
            "command_digest": None,
            "execution_location": "coding_runner",
            "execution_location_label": "Coding Runner",
            "target_ref": run.runner_server,
            "working_directory": "Legacy isolated worktree",
            "working_directory_label": "Legacy isolated worktree",
            "status": status,
            "status_source": "job",
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": (
                safe_engineering_exit_code(job.exit_code)
                if status in {"done", "failed", "cancelled"}
                else None
            ),
            "duration_seconds": None,
            "policy_family": "legacy_unknown",
            "policy_disposition": "legacy_unknown",
            "approval": {"required": False, "approval_id": run.approval_id},
            "log": {"available": False, "url": None},
            "origin": "legacy_snapshot",
        }
    ]


def engineering_artifact_to_dict(artifact: EngineeringTaskArtifact) -> dict:
    return {
        "id": artifact.id,
        "attempt_number": artifact.attempt_number,
        "artifact_key": artifact.artifact_key,
        "kind": artifact.kind,
        "label": artifact.label,
        "storage_kind": artifact.storage_kind,
        "storage_key": artifact.storage_key,
        "content_type": artifact.content_type,
        "sha256": artifact.source_sha256,
        "size_bytes": artifact.source_size_bytes,
        "verification_status": artifact.verification_status,
        "redaction_status": artifact.redaction_status,
        "availability": artifact.availability,
        "created_at": artifact.created_at,
        "collected_at": artifact.collected_at,
        "updated_at": artifact.updated_at,
    }


def engineering_validation_request_to_dict(
    app_state: Any, validation: EngineeringValidationRequest
) -> dict:
    """Safe projection: never expose command, instance path, or SSH identity."""

    linked_job_id = validation.downstream_job_id or validation.bundle_push_job_id
    linked_job = app_state.db.get_job(linked_job_id) if linked_job_id is not None else None
    connection = {"code": "not_started", "reason": None}
    if linked_job is not None and linked_job.status not in VALID_STATUSES:
        connection = {"code": "unknown", "reason": "job_status_unrecognized"}
    elif linked_job is not None and linked_job.status in {"queued", "running"}:
        failure = engineering_validation_job_contract_failure(
            app_state.db,
            linked_job,
            app_state.server_configs,
            local_home_dir=app_state.config.local_home_dir,
        )
        target_state = app_state.server_states.get(validation.target_server)
        if failure is not None:
            connection = {"code": "disconnected", "reason": failure}
        elif target_state is None:
            connection = {"code": "unknown", "reason": "worker_state_unobserved"}
        elif not target_state.online:
            connection = {"code": "offline", "reason": "worker_offline"}
        else:
            connection = {"code": "contract_valid", "reason": None}
    elif linked_job is not None:
        connection = {"code": "observed_terminal", "reason": None}

    validation_statuses = VALID_STATUSES | {
        "pending_approval",
        "rejected",
        "unknown",
    }
    safe_status = (
        validation.status if validation.status in validation_statuses else "unknown"
    )
    safe_result_status = validation.result_status
    if safe_result_status is not None and safe_result_status not in (
        VALID_STATUSES | {"unknown"}
    ):
        safe_result_status = "unknown"
    terminal_result_statuses = {"done", "failed", "cancelled"}
    safe_result_exit_code = None
    safe_result_finished_at = None
    if safe_result_status in terminal_result_statuses:
        raw_exit_code = validation.result_exit_code
        if (
            isinstance(raw_exit_code, int)
            and not isinstance(raw_exit_code, bool)
            and 0 <= raw_exit_code <= 255
        ):
            safe_result_exit_code = raw_exit_code
        raw_finished_at = validation.result_finished_at
        if isinstance(raw_finished_at, str) and 0 < len(raw_finished_at) <= 64:
            try:
                parsed_finished_at = datetime.fromisoformat(raw_finished_at)
            except ValueError:
                parsed_finished_at = None
            if (
                parsed_finished_at is not None
                and parsed_finished_at.tzinfo is not None
                and parsed_finished_at.utcoffset() is not None
            ):
                safe_result_finished_at = raw_finished_at

    return {
        "id": validation.id,
        "engineering_task_id": validation.engineering_task_id,
        "attempt_number": validation.attempt_number,
        "coding_run_id": validation.coding_run_id,
        "approval_id": validation.approval_id,
        "project_id": validation.project_id,
        "project": validation.project_name,
        "project_version_id": validation.project_version_id,
        "base_commit": validation.base_commit,
        "result_commit": validation.result_commit,
        "target_server": validation.target_server,
        "status": safe_status,
        "bundle_push_job_id": validation.bundle_push_job_id,
        "downstream_job_id": validation.downstream_job_id,
        "result": {
            "status": safe_result_status,
            "exit_code": safe_result_exit_code,
            "finished_at": safe_result_finished_at,
        },
        "connection": connection,
        "created_at": validation.created_at,
        "updated_at": validation.updated_at,
    }


def engineering_artifact_snapshots(app_state: Any, run: Optional[CodingRun]) -> list[dict]:
    if run is None or run.job_id is None:
        return []
    result_dir = local_result_dir(run.job_id, app_state.config.local_home_dir)
    snapshots: list[dict] = []
    for key, kind, label, filename, include_text in (
        ("result-metadata", "result_metadata", "Result metadata", "result.json", False),
        ("final-response", "final_response", "Final response", "final_message.txt", True),
        ("diff", "diff", "Code diff", "diff.patch", True),
        ("bundle", "bundle", "Change bundle", "changes.bundle", False),
    ):
        inspected = inspect_engineering_result_file(
            result_dir=result_dir,
            filename=filename,
            include_text=include_text,
        )
        if not inspected.get("available"):
            continue
        if kind == "bundle":
            verification = (
                "verified"
                if run.base_binding == "project_version_pinned" and bool(run.bundle_path)
                else "unknown"
            )
            redaction = "not_applicable"
            availability = "available"
        elif kind == "result_metadata":
            verification = "not_required"
            redaction = "withheld"
            availability = "available"
        else:
            verification = "not_required"
            redaction = "withheld" if inspected.get("withheld") else "redacted"
            availability = "withheld" if inspected.get("withheld") else "available"
        snapshots.append(
            {
                "id": f"snapshot-{run.id}-{key}",
                "attempt_number": run.attempt_number,
                "artifact_key": key,
                "kind": kind,
                "label": label,
                "storage_kind": "local_result",
                "storage_key": filename,
                "content_type": None,
                "sha256": inspected.get("sha256"),
                "size_bytes": inspected.get("size_bytes"),
                "verification_status": verification,
                "redaction_status": redaction,
                "availability": availability,
                "created_at": run.finished_at or run.created_at,
                "collected_at": None,
                "updated_at": None,
                "origin": "legacy_snapshot" if run.engineering_task_id is None else "snapshot_adapter",
            }
        )
    return snapshots


def engineering_test_summary(run: Optional[CodingRun]) -> dict:
    if run is None or run.test_command is None:
        return {"status": "not_run", "label": "未執行", "exit_code": None}
    exit_code = safe_engineering_exit_code(run.test_exit_code)
    if exit_code is None:
        return {"status": "unknown", "label": "結果未知", "exit_code": None}
    if exit_code == 0:
        return {"status": "passed", "label": "通過", "exit_code": 0}
    return {"status": "failed", "label": "失敗", "exit_code": exit_code}


def coding_run_to_dict(run: CodingRun) -> dict:
    data = {
        "id": run.id,
        "approval_id": run.approval_id,
        "job_id": run.job_id,
        "project": run.project,
        "runner_server": run.runner_server,
        "instruction": run.instruction,
        "base_branch": run.base_branch,
        "base_commit": run.base_commit,
        "result_branch": run.result_branch,
        "result_commit": run.result_commit,
        "has_bundle": bool(run.bundle_path),
        "validation_target": run.validation_target,
        "codex_version": run.codex_version,
        "status": run.status,
        "test_command": run.test_command,
        "test_exit_code": run.test_exit_code,
        "engineering_task_id": run.engineering_task_id,
        "project_version_id": run.project_version_id,
        "base_binding": run.base_binding,
        "attempt_number": run.attempt_number,
        "created_at": run.created_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "error_message": run.error_message,
    }
    # Preserve the legacy response shape while preventing old Runner-controlled
    # rows from becoming a credential or private-path read endpoint.
    for key, limit in (
        ("instruction", 4096),
        ("base_commit", 128),
        ("result_branch", 256),
        ("result_commit", 128),
        ("status", 128),
        ("test_command", 512),
        ("error_message", 1024),
        ("codex_version", 120),
    ):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        preview = redact_engineering_text(value, max_chars=limit)
        data[key] = (
            preview.get("content")
            if not preview.get("withheld")
            else "Sensitive details withheld"
        )
    return data


def engineering_coding_run_to_dict(run: CodingRun) -> dict:
    """Safe CodingRun projection for the new visibility surface.

    Legacy compatibility endpoints retain their established payload.  The new
    surface does not pass through untrusted Runner error/test strings.
    """

    data = coding_run_to_dict(run)
    data["status"] = safe_engineering_status(
        run.status, SAFE_ENGINEERING_CODING_RUN_STATUSES
    )
    data["test_exit_code"] = safe_engineering_exit_code(run.test_exit_code)
    instruction_preview = redact_engineering_text(run.instruction, max_chars=4096)
    data["instruction"] = (
        None
        if instruction_preview.get("withheld")
        else instruction_preview.get("content")
    )
    data["instruction_visibility"] = {
        "redacted": bool(instruction_preview.get("redacted")),
        "withheld": bool(instruction_preview.get("withheld")),
        "truncated": bool(instruction_preview.get("truncated")),
    }
    if run.test_command not in (None, "python3 -m pytest -q"):
        data["test_command"] = "Validation command (details withheld)"
    if run.error_message:
        preview = redact_engineering_text(run.error_message, max_chars=1024)
        data["error_message"] = (
            preview.get("content") if not preview.get("withheld") else "Sensitive error details withheld"
        )
    return data


#: `final_message`/`diff_patch` 截斷上限（PLAN.md N.6：「≤64KB 截斷」）。
CODING_RUN_FILE_MAX_CHARS = 65536


def read_local_coding_result_file(
    app_state: Any, job_id: Optional[int], filename: str
) -> Optional[str]:
    """讀本地已由既有 E 節結果回收拉回的 `results/{job_id}/{filename}`
    （`final_message.txt`／`diff.patch`）。`job_id` 是 None（coding_run 還
    沒回填 job_id，理論上不會發生但防禦性處理）、檔案不存在、或任何讀取
    錯誤都回傳 `None`，不丟例外——這是輔助顯示用的附加內容，缺失不代表
    coding_run 本身有問題。檔案透過同一個 bounded/no-follow inspector
    讀取、去敏，再依 `CODING_RUN_FILE_MAX_CHARS` 截斷。
    """
    if job_id is None:
        return None
    path = Path(local_result_dir(job_id, app_state.config.local_home_dir)) / filename
    inspected = inspect_engineering_result_file(
        result_dir=str(path.parent),
        filename=filename,
        include_text=True,
        max_chars=CODING_RUN_FILE_MAX_CHARS,
    )
    if not inspected.get("available") or inspected.get("withheld"):
        return None
    content = inspected.get("content")
    return content if isinstance(content, str) else None


def engineering_runner_connection(app_state: Any, runner_server: Optional[str]) -> dict:
    if not runner_server:
        return {
            "code": "unknown",
            "label": "Runner 未知",
            "observed_at": None,
            "reason": "runner_not_recorded",
        }
    state = app_state.server_states.get(runner_server)
    if state is None:
        return {
            "code": "unknown",
            "label": "尚無連線觀測",
            "observed_at": None,
            "reason": "not_observed",
        }
    if not state.online and (state.updated_at is not None or state.error):
        return {
            "code": "disconnected",
            "label": "Runner 連線中斷",
            "observed_at": state.updated_at,
            "reason": "monitor_offline",
        }
    if state.updated_at is None:
        return {
            "code": "unknown",
            "label": "尚無連線觀測",
            "observed_at": None,
            "reason": "not_observed",
        }
    return {
        "code": "connected",
        "label": "Runner 已連線",
        "observed_at": state.updated_at,
        "reason": None,
    }


def engineering_task_presentation_flags(
    app_state: Any, task: EngineeringTask
) -> dict[str, bool]:
    """Combine full-journal facts with the current non-secret Runner identity."""

    flags = app_state.db.get_engineering_task_presentation_flags(task.id)
    mismatch_observed = flags.get("runner_contract_mismatch") is True
    mismatch_active = False
    if mismatch_observed:
        approved_runner = (
            task.execution_contract.get("runner")
            if isinstance(task.execution_contract, dict)
            else None
        )
        current = app_state.server_configs.get(task.runner_server)
        current_runner = (
            {
                "name": current.name,
                "host": current.host,
                "user": current.user,
                "port": current.port,
            }
            if current is not None and current.enabled
            else None
        )
        mismatch_active = not isinstance(approved_runner, dict) or (
            approved_runner != current_runner
        )
    return {**flags, "runner_contract_mismatch_active": mismatch_active}


def engineering_presentation(
    app_state: Any,
    *,
    task_data: dict,
    approval: Optional[Approval],
    run: Optional[CodingRun],
    jobs: list[Job],
    events: list[dict],
    event_flags: Optional[dict[str, bool]] = None,
) -> dict:
    warnings: list[str] = []
    jobs_by_role = {job.engineering_task_role: job for job in jobs}
    staging = jobs_by_role.get("staging")
    coding = jobs_by_role.get("coding")
    run_status = (
        safe_engineering_status(run.status, SAFE_ENGINEERING_CODING_RUN_STATUSES)
        if run is not None
        else "unknown"
    )
    if approval is not None and approval.status == "pending":
        state = "pending_approval"
        phase = "approval"
    elif approval is not None and approval.status == "rejected":
        state = "rejected"
        phase = "approval"
    elif task_data.get("status") == "discarded":
        # Discard only ever transitions from a terminal task state (D3
        # request/approve eligibility both require it) and never mutates the
        # owner Jobs/CodingRun those statuses came from; presenting this
        # ahead of the Job-driven branches below keeps "discarded" visible
        # rather than reverting to whatever terminal state preceded it.
        state = "discarded"
        phase = "complete"
    elif task_data.get("legacy"):
        state = run_status
        phase = (
            "complete"
            if state
            in {
                "done",
                "no_changes",
                "failed",
                "secret_violation",
                "path_policy_violation",
            }
            else "execution" if state == "running" else "queue"
        )
    elif staging is None or coding is None:
        state = "unknown"
        phase = "unknown"
        warnings.append("approved task is missing one or more owner Jobs")
    elif staging.status not in VALID_STATUSES or coding.status not in VALID_STATUSES:
        state = "unknown"
        phase = "unknown"
        warnings.append("Owner Job status is unrecognized")
    elif staging.status in {"failed", "blocked", "cancelled"}:
        state = "staging_failed" if staging.status == "failed" else staging.status
        phase = "staging"
    elif staging.status == "running":
        state = "staging"
        phase = "staging"
    elif staging.status != "done":
        state = "queued"
        phase = "queue"
    elif coding.status == "queued":
        state = "queued"
        phase = "queue"
    elif coding.status == "running":
        state = "running"
        phase = "execution"
    elif coding.status in {"failed", "blocked", "cancelled"} and run is not None and (
        run_status in {"done", "no_changes"}
    ):
        state = "unknown"
        phase = "unknown"
        warnings.append("Job terminal state contradicts the collected CodingRun result")
    elif coding.status in {"failed", "blocked", "cancelled"} and (
        run is None
        or run_status
        not in {
            "done",
            "no_changes",
            "secret_violation",
            "path_policy_violation",
        }
    ):
        state = coding.status
        phase = "complete"
    elif coding.status == "done" and (
        run is None
        or run_status
        not in {
            "done",
            "no_changes",
            "failed",
            "secret_violation",
            "path_policy_violation",
        }
    ):
        state = "finalizing"
        phase = "result_collection"
    elif run is not None and run_status in {"done", "no_changes", "failed"}:
        state = run_status
        phase = "complete"
    elif run is not None and run_status == "secret_violation":
        state = "secret_violation"
        phase = "complete"
        warnings.append("Runner result was rejected by the safety check")
    elif run is not None and run_status == "path_policy_violation":
        state = "path_policy_violation"
        phase = "complete"
        warnings.append("Runner result was rejected by the enforced path policy")
    else:
        state = "unknown"
        phase = "unknown"
        warnings.append("Job and CodingRun evidence is incomplete or contradictory")

    event_flags = event_flags or {}
    interrupted = bool(event_flags.get("execution_interrupted")) or any(
        event.get("type") == "execution_interrupted" for event in events
    )
    runner_contract_mismatch_observed = bool(
        event_flags.get("runner_contract_mismatch")
    ) or any(event.get("type") == "runner_contract_mismatch" for event in events)
    runner_contract_mismatch_active = bool(
        event_flags.get("runner_contract_mismatch_active")
    )
    if runner_contract_mismatch_active:
        warnings.append(
            "Coding Runner 設定已與核准 execution contract 不同；平台未連線"
        )
    elif runner_contract_mismatch_observed:
        warnings.append(
            "Coding Runner execution contract 曾不一致；目前設定已恢復，歷史事件仍保留"
        )
    if state in {
        "failed",
        "staging_failed",
        "secret_violation",
        "path_policy_violation",
    }:
        health_code, health_label = "failed", "執行失敗"
    elif state == "blocked":
        health_code, health_label = "blocked", "執行受阻"
    elif state == "cancelled":
        health_code, health_label = "cancelled", "執行已取消"
    elif state in {"pending_approval", "rejected"}:
        health_code, health_label = "not_started", "尚未執行"
    elif state == "unknown":
        health_code, health_label = "unknown", "執行健康度未知"
    elif runner_contract_mismatch_active and state in {
        "queued",
        "running",
        "finalizing",
    }:
        health_code, health_label = "disconnected", "Runner execution contract 已中斷"
    elif interrupted and state == "queued":
        health_code, health_label = "interrupted", "曾中斷，等待重試"
    elif state in {"done", "no_changes"}:
        health_code, health_label = "success", "執行成功"
    else:
        health_code, health_label = "healthy", "無已知執行錯誤"

    raw_cached = task_data.get("status")
    cached = safe_engineering_status(raw_cached, SAFE_ENGINEERING_TASK_STATUSES)
    if raw_cached != cached:
        warnings.append("Cached task status is unrecognized")
    if not task_data.get("legacy") and cached not in {
        state,
        "secret_violation",
        "path_policy_violation",
    }:
        warnings.append("Cached task status differs from source evidence")
    runner_connection = engineering_runner_connection(
        app_state, task_data.get("runner_server")
    )
    if runner_contract_mismatch_active:
        runner_connection = {
            "code": "disconnected",
            "label": "Runner execution contract 不一致",
            "observed_at": task_data.get("updated_at"),
            "reason": "runner_contract_mismatch",
        }
    return {
        "state": {
            "code": state,
            "label": ENGINEERING_STATE_LABELS.get(state, "狀態未知"),
        },
        "phase": {
            "code": phase,
            "label": ENGINEERING_PHASE_LABELS.get(phase, "未知"),
        },
        "execution_health": {
            "code": health_code,
            "label": health_label,
            "reason": warnings[0] if warnings else None,
            "evidence_at": task_data.get("updated_at"),
        },
        "runner_connection": runner_connection,
        "warnings": warnings,
    }


def engineering_cleanup_availability(app_state: Any, run: Optional[CodingRun]) -> dict:
    if run is None:
        return {"enabled": False, "reason": "Coding Run 尚未建立", "coding_run_id": None}
    if run.status not in {
        "done",
        "failed",
        "no_changes",
        "secret_violation",
        "path_policy_violation",
    }:
        return {
            "enabled": False,
            "reason": "Coding Run 尚未到達終態",
            "coding_run_id": run.id,
        }
    if not run.worktree_path:
        return {
            "enabled": False,
            "reason": "隔離 worktree 已清理或不存在",
            "coding_run_id": run.id,
        }
    downstream = sorted(
        job.id
        for job in app_state.db.list_jobs(status="queued")
        + app_state.db.list_jobs(status="running")
        if job.source_coding_run_id == run.id
    )
    if downstream:
        return {
            "enabled": False,
            "reason": "尚有下游 Job 使用這份 change bundle",
            "coding_run_id": run.id,
        }
    if run.engineering_task_id is not None:
        owner_job = app_state.db.get_job(run.job_id) if run.job_id is not None else None
        owner_task = app_state.db.get_engineering_task(run.engineering_task_id)
        #: The Codex workspace root is gone with the runner config
        #: (DG-CONSOLIDATION-v1 C-5); cleanup is never offered any more.
        current_workspace = None
        if (
            owner_job is None
            or owner_task is None
            or owner_job.engineering_task_id != run.engineering_task_id
            or owner_task.coding_run_id != run.id
            or run.runner_server != owner_task.runner_server
            or owner_task.execution_contract.get("workspace_rel")
            != current_workspace
            or app_state._result_collection_server_config(owner_job) is None
        ):
            return {
                "enabled": False,
                "reason": "Coding Runner 設定已與核准的執行合約不同",
                "coding_run_id": run.id,
            }
    return {"enabled": True, "reason": None, "coding_run_id": run.id}


ENGINEERING_PATCH_NOT_FOUND = "native AI Engineering Task 不存在"
ENGINEERING_PATCH_UNAVAILABLE = "AI Engineering Task patch 尚未可下載"
ENGINEERING_PATCH_INTEGRITY_FAILURE = "AI Engineering Task patch 完整性驗證失敗"
ENGINEERING_PATCH_TOO_LARGE = "AI Engineering Task patch 超過 1 MiB 下載上限"
ENGINEERING_PATCH_WITHHELD = "AI Engineering Task patch 因安全政策而隱藏"


class EngineeringPatchDownloadError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def engineering_task_approved_payload(task: EngineeringTask) -> dict:
    contract = task.execution_contract
    return {
        "contract_version": task.contract_version,
        "engineering_task_id": task.id,
        "project": task.project_name,
        "project_id": task.project_id,
        "project_version_id": task.project_version_id,
        "base_commit": task.base_commit,
        "agent_provider_id": task.agent_provider_id,
        "provider_capabilities": task.provider_capabilities,
        "execution_contract": contract,
        "structured_request": task.structured_request,
        "instruction": task.instruction,
        "detected_metadata": task.detected_metadata,
        "validation_target": task.validation_target,
        "runner_server": task.runner_server,
        "source_kind": contract.get("source_kind"),
        "source": contract.get("source"),
        "network_access": False,
        "dependency_installation": False,
    }


def engineering_artifact_descriptor_is_complete(
    artifact: EngineeringTaskArtifact,
) -> bool:
    return (
        isinstance(artifact.source_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", artifact.source_sha256) is not None
        and isinstance(artifact.source_size_bytes, int)
        and not isinstance(artifact.source_size_bytes, bool)
        and artifact.source_size_bytes >= 0
        and artifact.collected_at is not None
    )


def engineering_patch_execution_contract_is_valid(task: EngineeringTask) -> bool:
    """Re-derive the safe, versioned parts of a native task contract.

    Comparing the task row with its approval detects ordinary drift, but both
    records are database state.  The task-local bundle key and v2 path-policy
    digest are independently derivable, so validate those facts again before a
    collected patch can be downloaded.
    """

    contract = task.execution_contract
    try:
        expected_source = remote_engineering_bundle_path(task.id)
    except ValueError:
        return False
    runner = contract.get("runner") if type(contract) is dict else None
    if (
        type(contract) is not dict
        or contract.get("source_kind") != "hub_bundle"
        or contract.get("source") != expected_source
        or contract.get("network_access") is not False
        or contract.get("dependency_installation") is not False
        or type(runner) is not dict
        or set(runner) != {"name", "host", "user", "port"}
        or runner.get("name") != task.runner_server
        or not isinstance(runner.get("host"), str)
        or not runner.get("host")
        or not isinstance(runner.get("user"), str)
        or not runner.get("user")
        or isinstance(runner.get("port"), bool)
        or not isinstance(runner.get("port"), int)
        or not 1 <= runner["port"] <= 65535
    ):
        return False
    try:
        workspace = contract.get("workspace_rel")
        if (
            not isinstance(workspace, str)
            or resolve_codex_workspace_rel(workspace) != workspace
        ):
            return False
    except ValueError:
        return False

    policy_keys = {"path_policy", "path_policy_sha256", "path_verifier"}
    if task.contract_version == "engineering-task-v1":
        return not any(key in contract for key in policy_keys)
    if task.contract_version != "engineering-task-v2":
        return False
    structured = task.structured_request
    if type(structured) is not dict:
        return False
    path_policy = contract.get("path_policy")
    path_policy_sha256 = contract.get("path_policy_sha256")
    path_verifier = contract.get("path_verifier")
    if (
        not isinstance(path_policy, Mapping)
        or not isinstance(path_policy_sha256, str)
        or not isinstance(path_verifier, Mapping)
    ):
        # the validators reject these shapes with EngineeringPathPolicyError
        # anyway; the explicit check keeps the same fail-closed result typed.
        return False
    try:
        policy = validate_engineering_path_policy(path_policy, path_policy_sha256)
        verifier = validate_engineering_path_verifier_contract(path_verifier)
    except EngineeringPathPolicyError:
        return False
    return (
        policy.get("verifier") == verifier
        and policy.get("allowed_paths") == structured.get("allowed_paths")
        and policy.get("prohibited_paths") == structured.get("prohibited_paths")
    )


def prepare_sanitized_collected_patch(app_state: Any, task_id: str) -> dict:
    task = app_state.db.get_engineering_task(task_id)
    if task is None:
        raise EngineeringPatchDownloadError(404, ENGINEERING_PATCH_NOT_FOUND)
    if task.status != "done" or task.coding_run_id is None:
        raise EngineeringPatchDownloadError(409, ENGINEERING_PATCH_UNAVAILABLE)
    if not engineering_patch_execution_contract_is_valid(task):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )

    approval = app_state.db.get_approval(task.approval_id)
    project = app_state.db.get_project(task.project_id)
    version = app_state.db.get_project_version(task.project_version_id)
    if (
        approval is None
        or approval.kind != "coding_task"
        or approval.status != "approved"
        or approval.payload != engineering_task_approved_payload(task)
        or project is None
        or project.id != task.project_id
        or project.name != task.project_name
        or version is None
        or version.project_id != task.project_id
        or version.project_name != task.project_name
        or version.git_commit != task.base_commit
    ):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )

    run = app_state.db.get_coding_run(task.coding_run_id)
    if run is None or run.status != "done" or run.job_id is None:
        raise EngineeringPatchDownloadError(409, ENGINEERING_PATCH_UNAVAILABLE)
    expected_result_dir = local_result_dir(
        run.job_id, app_state.config.local_home_dir
    )
    expected_bundle_path = str(Path(expected_result_dir) / "changes.bundle")
    if (
        run.approval_id != task.approval_id
        or run.project != task.project_name
        or run.runner_server != task.runner_server
        or run.instruction != task.instruction
        or run.validation_target != task.validation_target
        or run.engineering_task_id != task.id
        or run.attempt_number is None
        or run.attempt_number < 1
        or run.base_binding != "project_version_pinned"
        or run.project_version_id != task.project_version_id
        or run.base_commit != task.base_commit
        or re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", run.result_commit or "")
        is None
        or run.bundle_path != expected_bundle_path
    ):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )

    owner_job = app_state.db.get_job(run.job_id)
    command = app_state.db.get_engineering_task_command_by_job_id(run.job_id)
    if (
        owner_job is None
        or owner_job.type != "coding"
        or owner_job.project != task.project_name
        or owner_job.pin_server != task.runner_server
        or owner_job.server != task.runner_server
        or owner_job.status != "done"
        or owner_job.engineering_task_id != task.id
        or owner_job.engineering_task_role != "coding"
        or owner_job.engineering_attempt_number != run.attempt_number
        or command is None
        or command.engineering_task_id != task.id
        or command.attempt_number != run.attempt_number
        or command.job_id != owner_job.id
        or command.coding_run_id != run.id
        or command.command_role != "agent_turn"
        or command.execution_location != "coding_runner"
        or command.target_ref != task.runner_server
        or command.policy_disposition != "task_approved"
        or command.approval_id != task.approval_id
        or command.status_source != "job"
        or command.command_digest
        != hashlib.sha256(owner_job.command.encode("utf-8")).hexdigest()
    ):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )

    artifacts = app_state.db.list_engineering_task_artifacts(
        task.id, attempt_number=run.attempt_number
    )
    bundle = next(
        (item for item in artifacts if item.artifact_key == "bundle"), None
    )
    diff = next((item for item in artifacts if item.artifact_key == "diff"), None)
    if bundle is None or diff is None:
        raise EngineeringPatchDownloadError(409, ENGINEERING_PATCH_UNAVAILABLE)
    if (
        bundle.kind != "bundle"
        or bundle.storage_kind != "local_result"
        or bundle.storage_key != "changes.bundle"
        or bundle.content_type != "application/x-git-bundle"
        or bundle.coding_run_id != run.id
        or bundle.source_job_id != owner_job.id
        or bundle.attempt_number != run.attempt_number
        or bundle.verification_status != "verified"
        or bundle.redaction_status != "not_applicable"
        or bundle.availability != "available"
        or not engineering_artifact_descriptor_is_complete(bundle)
    ):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )
    if (
        diff.kind != "diff"
        or diff.storage_kind != "local_result"
        or diff.storage_key != "diff.patch"
        or diff.content_type != "text/x-diff"
        or diff.coding_run_id != run.id
        or diff.source_job_id != owner_job.id
        or diff.attempt_number != run.attempt_number
        or diff.verification_status != "not_required"
        or not engineering_artifact_descriptor_is_complete(diff)
    ):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )
    if diff.redaction_status == "withheld" or diff.availability == "withheld":
        raise EngineeringPatchDownloadError(409, ENGINEERING_PATCH_WITHHELD)
    if diff.redaction_status != "redacted" or diff.availability != "available":
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )
    if diff.source_size_bytes > ENGINEERING_TASK_SOURCE_FILE_LIMIT:
        raise EngineeringPatchDownloadError(413, ENGINEERING_PATCH_TOO_LARGE)

    captured = capture_sanitized_engineering_patch(result_dir=expected_result_dir)
    reason = captured.get("reason")
    if not captured.get("available"):
        if reason == "source_too_large":
            raise EngineeringPatchDownloadError(413, ENGINEERING_PATCH_TOO_LARGE)
        if reason in {
            "content_withheld",
            "invalid_payload",
            "sanitized_too_large",
        }:
            raise EngineeringPatchDownloadError(409, ENGINEERING_PATCH_WITHHELD)
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )
    payload = captured.get("_sanitized_payload")
    if (
        captured.get("sha256") != diff.source_sha256
        or captured.get("size_bytes") != diff.source_size_bytes
        or not isinstance(payload, bytes)
        or not payload
        or len(payload) > ENGINEERING_TASK_SOURCE_FILE_LIMIT
        or captured.get("withheld")
        or captured.get("truncated")
    ):
        raise EngineeringPatchDownloadError(
            409, ENGINEERING_PATCH_INTEGRITY_FAILURE
        )
    return {
        "task_id": task.id,
        "payload": payload,
        "redacted": bool(captured.get("redacted")),
    }


def engineering_patch_download_availability(app_state: Any, task_data: dict) -> dict:
    action = {
        "enabled": False,
        "reason": ENGINEERING_PATCH_UNAVAILABLE,
        "url": None,
        "artifact_kind": "sanitized_collected_patch",
    }
    if task_data.get("legacy"):
        action["reason"] = ENGINEERING_PATCH_NOT_FOUND
        return action
    task_id = task_data.get("id")
    if not isinstance(task_id, str):
        return action
    try:
        prepare_sanitized_collected_patch(app_state, task_id)
    except EngineeringPatchDownloadError as exc:
        action["reason"] = exc.detail
        return action
    action.update(
        {
            "enabled": True,
            "reason": None,
            "url": f"/api/v2/engineering-tasks/{task_id}/patch",
        }
    )
    return action


def engineering_retry_or_discard_availability(app_state: Any, task_data: dict) -> dict:
    """Shared eligibility presentation for D3's retry/discard actions.

    Mirrors the authoritative request-time check in
    ``app.approvals.request_engineering_task_retry_approval`` /
    ``request_engineering_task_discard_approval`` (task must be native,
    backend enabled, status terminal) so the button disables for the same
    reason the request endpoint would reject it.  This is presentation
    only: the request/approve boundary re-validates independently and is
    the actual authority.
    """

    if task_data.get("legacy"):
        return {"enabled": False, "reason": "legacy Coding Run 沒有這個動作"}
    if not _ENGINEERING_TASK_BACKEND_ENABLED:
        return {"enabled": False, "reason": "AI Engineering Task backend 未啟用"}
    status = task_data.get("status")
    if status not in CODING_RUN_TERMINAL_STATUSES:
        return {
            "enabled": False,
            "reason": f"task 目前狀態（{status}）不是終態，尚不能操作",
        }
    return {"enabled": True, "reason": None}


def engineering_available_actions(
    app_state: Any, task_data: dict, run: Optional[CodingRun]
) -> dict:
    unsupported = {
        key: {"enabled": False, "reason": "此動作需要後續受控執行切片"}
        for key in (
            "continue",
            "request_changes",
            "cancel",
            "finalize",
            "create_draft_pr",
        )
    }
    legacy = bool(task_data.get("legacy"))
    validation = bool(run and run.status == "done" and run.bundle_path)
    validation_reason = None if validation else "需要已驗證的 change bundle"
    if validation and not legacy and run is not None:
        task = app_state.db.get_engineering_task(str(task_data.get("id") or ""))
        approval = (
            app_state.db.get_approval(task.approval_id) if task is not None else None
        )
        artifact = next(
            (
                item
                for item in app_state.db.list_engineering_task_artifacts(
                    task.id, attempt_number=run.attempt_number
                )
                if item.kind == "bundle"
                and item.coding_run_id == run.id
                and item.verification_status == "verified"
                and item.availability == "available"
                and item.collected_at is not None
            ),
            None,
        ) if task is not None and run.attempt_number is not None else None
        inspected = (
            inspect_engineering_result_file(
                result_dir=local_result_dir(
                    run.job_id, app_state.config.local_home_dir
                ),
                filename="changes.bundle",
            )
            if run.job_id is not None
            else {"available": False}
        )
        if (
            not _ENGINEERING_TASK_BACKEND_ENABLED
            or task is None
            or approval is None
            or approval.kind != "coding_task"
            or approval.status != "approved"
            or task.coding_run_id != run.id
            or run.engineering_task_id != task.id
            or run.base_binding != "project_version_pinned"
            or run.project_version_id != task.project_version_id
            or run.base_commit != task.base_commit
            or artifact is None
            or not inspected.get("available")
            or artifact.source_sha256 != inspected.get("sha256")
            or artifact.source_size_bytes != inspected.get("size_bytes")
        ):
            validation = False
            validation_reason = "immutable task／bundle contract 尚未通過伺服器驗證"
    promotion = {
        "enabled": False,
        "reason": "Code promotion rollout flag 未啟用",
        "request_url": None,
    }
    if legacy:
        promotion["reason"] = "legacy Coding Run 沒有 immutable task contract"
    elif app_state.config.code_promotion_v1_enabled:
        try:
            resolve_promotion_candidate(
                app_state.db,
                app_state.config,
                str(task_data.get("id") or ""),
            )
        except PromotionCandidateError as exc:
            promotion["reason"] = str(exc)
        else:
            promotion = {
                "enabled": True,
                "reason": None,
                "request_url": (
                    f"/api/v2/engineering-tasks/{task_data['id']}/promote-requests"
                ),
            }
    return {
        "request_worker_validation": {
            "enabled": validation,
            "reason": validation_reason,
            "coding_run_id": run.id if run else None,
            "engineering_task_id": None if legacy else task_data.get("id"),
            "request_mode": "legacy_dispatch" if legacy else "native_pending_approval",
        },
        "download_patch": engineering_patch_download_availability(app_state, task_data),
        "download_bundle": {
            "enabled": False,
            "reason": (
                "raw bundle 可能包含未經去敏內容，因此目前 withheld；"
                "請使用 sanitized collected patch"
            ),
        },
        "cleanup": engineering_cleanup_availability(app_state, run),
        "retry": {
            # DG-AGENT-RUNTIME-V3 Phase 1b (R6): retry re-ran the retired
            # job-backed channel; the honest projection says so.
            "enabled": False,
            "reason": "已退役（DG-AGENT-RUNTIME-V3 Phase 1b）：請在 Studio session 繼續迭代，再 Checkpoint 產生新 bundle",
        },
        "discard": engineering_retry_or_discard_availability(app_state, task_data),
        "promote": promotion,
        **unsupported,
    }


def engineering_approval_history(
    app_state: Any, approvals: list[Optional[Approval]]
) -> list[dict]:
    """Project only safe identity and decision metadata for task history.

    Approval payloads can contain exact executor commands and other immutable
    execution details, so this projection intentionally cannot serialize them.
    The id-keyed reduction is defense in depth for callers that combine parent
    and linked validation sources themselves.
    """

    unique = {approval.id: approval for approval in approvals if approval is not None}
    ordered = sorted(
        unique.values(), key=lambda approval: (approval.created_at or "", approval.id)
    )

    def actor_projection(actor_id: Optional[str]) -> Optional[dict]:
        actor = app_state.db.get_actor(actor_id) if actor_id else None
        if actor is None:
            return None
        return {
            "id": actor.id,
            "type": actor.actor_type.value,
            "display_name": actor.display_name,
        }

    return [
        {
            "approval_id": approval.id,
            "kind": approval.kind,
            "status": approval.status,
            "created_at": approval.created_at,
            "decided_at": approval.decided_at,
            "requester": actor_projection(approval.requester_actor_id),
            "approver": actor_projection(approval.decision_actor_id),
            "decision_mechanism": approval.decision_mechanism,
        }
        for approval in ordered
    ]


def engineering_result_preview(
    app_state: Any, run: Optional[CodingRun], filename: str, *, max_chars: int = 65536
) -> dict:
    if run is None or run.job_id is None:
        return {
            "available": False,
            "reason": "coding_run_not_linked",
            "content": None,
            "redacted": False,
            "withheld": False,
            "truncated": False,
        }
    artifacts = (
        app_state.db.list_engineering_task_artifacts(
            run.engineering_task_id,
            attempt_number=run.attempt_number,
        )
        if run.engineering_task_id is not None and run.attempt_number is not None
        else []
    )
    refusal = engineering_artifact_preview_refusal(
        run,
        filename,
        artifacts,
    )
    if refusal is not None:
        return refusal
    inspected = inspect_engineering_result_file(
        result_dir=local_result_dir(run.job_id, app_state.config.local_home_dir),
        filename=filename,
        include_text=True,
        max_chars=max_chars,
    )
    refusal = engineering_artifact_preview_refusal(
        run,
        filename,
        artifacts,
        inspected=inspected,
    )
    return refusal if refusal is not None else inspected


def engineering_artifact_preview_refusal(
    run: CodingRun,
    filename: str,
    artifacts: list[EngineeringTaskArtifact],
    *,
    inspected: Optional[dict] = None,
) -> Optional[dict]:
    """Fail closed when native artifact evidence says content is not visible.

    Result files are Runner-controlled inputs.  Once Server A has persisted a
    canonical visibility row, every detail/compatibility projection must honor
    that row instead of independently reopening the same logical file.  A
    path/secret-policy terminal status also withholds a diff even if collection
    crashed before the additive artifact journal was written.

    Native content requires a complete canonical artifact row.  The current
    file is read through the safe descriptor inspector and its source digest
    and size must still match the immutable collection descriptor before any
    text is returned.  Legacy CodingRuns take their separate compatibility
    path and retain their existing behavior.
    """

    if run.engineering_task_id is None:
        return None

    def refusal(reason: str) -> dict:
        return {
            "available": False,
            "reason": reason,
            "content": None,
            "redacted": False,
            "withheld": True,
            "truncated": False,
        }

    if filename == "diff.patch" and run.status in {
        "secret_violation",
        "path_policy_violation",
    }:
        return refusal("artifact_policy_withheld")

    expected = {
        "diff.patch": ("diff", "diff", "not_required", "redacted"),
        "final_message.txt": (
            "final-response",
            "final_response",
            "not_required",
            "redacted",
        ),
    }.get(filename)
    if expected is None:
        return None

    artifact_key, kind, verification, redaction = expected
    matches = [
        artifact
        for artifact in artifacts
        if artifact.artifact_key == artifact_key
        and artifact.kind == kind
        and artifact.storage_kind == "local_result"
        and artifact.storage_key == filename
        and artifact.engineering_task_id == run.engineering_task_id
        and artifact.attempt_number == run.attempt_number
        and artifact.coding_run_id == run.id
        and artifact.source_job_id == run.job_id
    ]
    if len(matches) != 1:
        return refusal("artifact_visibility_unverified")
    artifact = matches[0]
    if (
        artifact.verification_status != verification
        or artifact.redaction_status != redaction
        or artifact.availability != "available"
    ):
        return refusal("artifact_policy_withheld")
    if not engineering_artifact_descriptor_is_complete(artifact):
        return refusal("artifact_integrity_unverified")
    if inspected is not None and (
        not inspected.get("available")
        or inspected.get("storage_key") != filename
        or inspected.get("sha256") != artifact.source_sha256
        or inspected.get("size_bytes") != artifact.source_size_bytes
    ):
        return refusal("artifact_integrity_unverified")
    return None


def engineering_diff_summary(preview: dict) -> Optional[str]:
    text = preview.get("content")
    if not isinstance(text, str) or not text:
        return None
    files: set[str] = set()
    additions = 0
    removals = 0
    for line in text.splitlines():
        if line.startswith(("+++ ", "--- ")):
            name = line[4:].strip()
            if name.startswith(("a/", "b/")):
                name = name[2:]
            if name and name != "/dev/null":
                files.add(name)
        elif line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            removals += 1
    return f"{len(files)} 個檔案變更，+{additions} -{removals}"


def engineering_attempts(
    app_state: Any,
    *,
    task_data: dict,
    run: Optional[CodingRun],
    jobs: list[Job],
    presentation: dict,
) -> list[dict]:

    if run is None:
        return []
    jobs_by_role = {job.engineering_task_role: job for job in jobs}
    coding_job = jobs_by_role.get("coding")
    if task_data.get("legacy") and run.job_id is not None:
        coding_job = app_state.db.get_job(run.job_id)
    staging_job = jobs_by_role.get("staging")
    return [
        {
            "attempt_number": run.attempt_number,
            "coding_run_id": run.id,
            "project_version_id": run.project_version_id,
            "base_binding": run.base_binding,
            "base_commit": (
                run.base_commit if run.base_binding == "project_version_pinned" else None
            ),
            "observed_base_commit": (
                run.base_commit if run.base_binding == "legacy_unpinned" else None
            ),
            "runner_server": run.runner_server,
            "staging_job_id": staging_job.id if staging_job else None,
            "coding_job_id": coding_job.id if coding_job else run.job_id,
            "state": presentation["state"],
            "phase": presentation["phase"],
            "health": presentation["execution_health"],
            "created_at": run.created_at,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "exit_code": safe_engineering_exit_code(
                coding_job.exit_code if coding_job else None
            ),
            "result_status": safe_engineering_status(
                run.status, SAFE_ENGINEERING_CODING_RUN_STATUSES
            ),
            "result_commit": run.result_commit,
            "test_summary": engineering_test_summary(run),
            "source": "legacy_snapshot" if task_data.get("legacy") else "coding_run",
        }
    ]


def build_engineering_task_detail(app_state: Any, task_id: str) -> dict:
    from fastapi import HTTPException  # local import: keep this module importable
    # without pulling FastAPI into pure-projection callers that never hit this
    # orchestrator (mirrors the narrow scope of the exception this raises).

    legacy_prefix = "legacy-coding-run-"
    if task_id.startswith(legacy_prefix):
        raw_id = task_id[len(legacy_prefix) :]
        if not raw_id.isdigit():
            raise HTTPException(status_code=404, detail="engineering task 不存在")
        run = app_state.db.get_coding_run(int(raw_id))
        if run is None or run.engineering_task_id is not None:
            raise HTTPException(status_code=404, detail="engineering task 不存在")
        data = legacy_coding_run_to_engineering_task(run)
        approval = app_state.db.get_approval(run.approval_id)
        jobs: list[Job] = []
        events = legacy_engineering_events(run)
        commands = legacy_engineering_command(app_state, run)
        artifacts = engineering_artifact_snapshots(app_state, run)
        worker_validations: list[dict] = []
        history_approvals: list[Optional[Approval]] = [approval]
    else:
        task = app_state.db.get_engineering_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="engineering task 不存在")
        data = engineering_task_to_dict(task)
        approval = app_state.db.get_approval(task.approval_id)
        run = (
            app_state.db.get_coding_run(task.coding_run_id)
            if task.coding_run_id is not None
            else None
        )
        jobs = app_state.db.list_engineering_task_jobs(task.id)
        events = [
            engineering_event_to_dict(event)
            for event in app_state.db.list_engineering_task_events(task.id, limit=100)
        ]
        commands = [
            engineering_command_to_dict(app_state, command)
            for command in app_state.db.list_engineering_task_commands(task.id)
        ]
        artifacts = [
            engineering_artifact_to_dict(artifact)
            for artifact in app_state.db.list_engineering_task_artifacts(task.id)
        ]
        if not artifacts:
            artifacts = engineering_artifact_snapshots(app_state, run)
        worker_validations = [
            engineering_validation_request_to_dict(app_state, refreshed)
            for item in app_state.db.list_engineering_validation_requests(task.id)
            if (
                refreshed := app_state.db.refresh_engineering_validation_request_status(
                    item.id
                )
            )
            is not None
        ]
        # A refresh can append a durable validation status transition.  Read
        # the journal again so this detail response includes the transition
        # that it just observed instead of delaying it until the next poll.
        events = [
            engineering_event_to_dict(event)
            for event in app_state.db.list_engineering_task_events(task.id, limit=100)
        ]
        history_approvals = list(app_state.db.list_engineering_task_approvals(task.id))

    presentation = engineering_presentation(
        app_state,
        task_data=data,
        approval=approval,
        run=run,
        jobs=jobs,
        events=events,
        event_flags=(
            None
            if data.get("legacy")
            else engineering_task_presentation_flags(app_state, task)
        ),
    )
    diff_preview = engineering_result_preview(app_state, run, "diff.patch", max_chars=65536)
    final_preview = engineering_result_preview(
        app_state, run, "final_message.txt", max_chars=16384
    )
    approval_history = engineering_approval_history(app_state, history_approvals)
    approval_entry = next(
        (
            entry
            for entry in approval_history
            if approval is not None and entry["approval_id"] == approval.id
        ),
        {},
    )
    data.update(
        {
            "coding_run": engineering_coding_run_to_dict(run) if run is not None else None,
            "presentation": presentation,
            "attempts": engineering_attempts(
                app_state,
                task_data=data,
                run=run,
                jobs=jobs,
                presentation=presentation,
            ),
            "events": events,
            "commands": commands,
            "tests": [engineering_test_summary(run)],
            "artifacts": artifacts,
            "worker_validations": worker_validations,
            "changes": {
                "available": bool(diff_preview.get("available"))
                and not bool(diff_preview.get("withheld")),
                "summary": engineering_diff_summary(diff_preview),
                "truncated": bool(diff_preview.get("truncated")),
                "redacted": bool(diff_preview.get("redacted")),
                "withheld": bool(diff_preview.get("withheld")),
                "diff_url": f"/api/v2/engineering-tasks/{task_id}/diff",
            },
            "final_response": {
                "available": bool(final_preview.get("available"))
                and not bool(final_preview.get("withheld")),
                "content": final_preview.get("content"),
                "redacted": bool(final_preview.get("redacted")),
                "withheld": bool(final_preview.get("withheld")),
                "truncated": bool(final_preview.get("truncated")),
            },
            "requester": approval_entry.get("requester"),
            "approver": approval_entry.get("approver"),
            "warnings": presentation["warnings"],
            "approval_history": approval_history,
            "available_actions": engineering_available_actions(app_state, data, run),
        }
    )
    return data
