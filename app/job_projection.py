"""Shared Job -> dict projection (DG-UI-UNIFICATION v1 U3).

Extracted verbatim from `app.main._job_to_dict` and its three private
engineering-masking helpers so both the legacy `/jobs` surface and the
`/api/v2/jobs` wrapper surface (`dispatch_center/api/routers/jobs_v2.py`)
project byte-identical output from one implementation.

This lives outside `app.main` on purpose: `app.main` imports its v2 routers
near the top of the module, long before any of its own function definitions
exist, so a router importing names back out of `app.main` would be a
circular import (see `app/auto_placement.py` / `app/capacity.py` for the
same "no `import app.main`" convention elsewhere in this codebase). `db` is
threaded through as a parameter instead of read from the module-global
`app_state.db` for the same reason — this module has no `app_state`.
"""

from __future__ import annotations

import hashlib

from app.db import Database, Job
from app.engineering_tasks import redact_engineering_text


def engineering_job_display_command(job: Job) -> str:
    """Return a semantic label without exposing an internal executor command."""

    if job.engineering_validation_request_id is not None:
        return (
            "Push verified Engineering Task bundle to approved worker"
            if job.type == "sync"
            else "Run approved Engineering Task worker validation"
        )
    return {
        "staging": "Prepare immutable Engineering Task inputs",
        "coding": "Run Codex agent in an isolated worktree",
        "validation": "Run approved Engineering Task validation",
    }.get(job.engineering_task_role or "", "Run Engineering Task step")


def engineering_protected_job(job: Job) -> bool:
    return (
        job.engineering_task_id is not None
        or job.engineering_validation_request_id is not None
    )


def engineering_job_log_preview(job: Job, *, max_chars: int = 65_536) -> dict:
    """Build the only compatibility-safe projection of an owner Job log."""

    preview = redact_engineering_text(job.log_tail or "", max_chars=max_chars)
    if preview.get("withheld"):
        preview["content"] = None
    return preview


def job_to_dict(job: Job, *, db: Database) -> dict:
    engineering_owned = engineering_protected_job(job)
    validation = (
        db.get_engineering_validation_request_by_job_id(job.id)
        if job.engineering_validation_request_id is not None
        else None
    )
    log_preview = (
        engineering_job_log_preview(job) if engineering_owned else None
    )
    data = {
        "id": job.id,
        "type": job.type,
        "project": job.project,
        "command": (
            engineering_job_display_command(job) if engineering_owned else job.command
        ),
        "require_tag": job.require_tag,
        "pin_server": job.pin_server,
        "depends_on": job.depends_on,
        "gpus_needed": job.gpus_needed,
        "status": job.status,
        "server": job.server,
        "priority": job.priority,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
        "log_tail": (
            log_preview.get("content") if log_preview is not None else job.log_tail
        ),
        "target_server": job.target_server,
        "dataset_name": job.dataset_name,
        "dataset_version": job.dataset_version,
        #: 階段 4：卡死偵測旗標（不影響 status，見 app/stall.py）。
        "stalled_suspect": bool(job.stalled_suspect),
        #: 階段 13（PLAN.md N.6/N.7）：這個 job 是否是「用某次 Codex
        #: coding run 的 changes.bundle 當起點」的下游任務，一般任務一律
        #: `None`。「job manifest」在現制＝jobs 欄位＋稽核（見 N.13），
        #: 這裡是那份 manifest 對外可見的一部分。
        "source_coding_run_id": job.source_coding_run_id,
        "engineering_task_id": job.engineering_task_id,
        "engineering_task_role": job.engineering_task_role,
        "engineering_attempt_number": job.engineering_attempt_number,
        "engineering_validation_request_id": job.engineering_validation_request_id,
        "validation_engineering_task_id": (
            validation.engineering_task_id if validation is not None else None
        ),
    }
    if engineering_owned and log_preview is not None:
        data.update(
            {
                "command_digest": hashlib.sha256(
                    job.command.encode("utf-8")
                ).hexdigest(),
                "execution_details_withheld": True,
                "log_redacted": bool(log_preview.get("redacted")),
                "log_withheld": bool(log_preview.get("withheld")),
                "log_truncated": bool(log_preview.get("truncated")),
            }
        )
    return data


__all__ = [
    "engineering_job_display_command",
    "engineering_protected_job",
    "engineering_job_log_preview",
    "job_to_dict",
]
