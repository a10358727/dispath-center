"""Engineering Task / Coding Run v2 read and mutation wrappers (DG-UI-
UNIFICATION v1 U6a).

Thin `/api/v2` wrappers around the exact legacy `/engineering-tasks*`,
`/coding-agents`, `/coding-runs*`, and `/projects/{name}/{engineering-tasks,
coding-task}-request*` surfaces in `app/main.py` -- modeled on `jobs_v2.py`/
`infrastructure_v2.py`/`projects_legacy_v2.py` (U3/U4/U5). Engineering Tasks
and Coding Runs stay legacy-scope `engineering_task`/`coding_run`/`platform`/
`project` objects, not a Product v2 typed contract: every mutating route here
only creates a pending `Approval` (`kind=engineering_task_retry/discard/
promote`, `kind=coding_task`, or a `worker_validation_request` row) decided
through the existing `POST /approve/{id}`/the v2 generic decision fan-out --
same engine, same authorization action, same audit -- exactly like
`jobs_v2.py`'s `stop-requests`/`dispatch-requests`. Gated only by
`api_v2_feature_gate` + `product_rbac_v2_feature_gate`, exactly like
`jobs_v2.py`/`infrastructure_v2.py`/`projects_legacy_v2.py`.

The security-sensitive Engineering Task / CodingRun redaction and
presentation pipeline (list/detail projection, diff/patch preparation, event/
command/artifact projections) is **not** duplicated here: it now lives in
`app/engineering_presentation.py` (see that module's docstring), a leaf
module extracted from `app/main.py` in this same packet so both the legacy
surface and this router share exactly one redaction implementation instead of
risking silent drift between two copies. This mirrors the
`app/job_projection.py` precedent `jobs_v2.py` already documents.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response

from app import approvals as approvals_module
from app import engineering_presentation
from app.approvals import (
    CodePromotionDisabledError,
    InvalidCodePromotionRequestError,
    request_engineering_task_promote_approval,
)
from app.authorization_enforce import filter_project_scoped
from app.engineering_tasks import (
    redact_engineering_text,
)
from app.db import VALID_STATUSES
from dispatch_center.api.errors import APIError
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)


logger = logging.getLogger(__name__)

ENGINEERING_TASK_CAPABILITIES_ROUTE = "/api/v2/engineering-tasks/capabilities"
CODING_AGENTS_ROUTE = "/api/v2/coding-agents"
ENGINEERING_TASK_LIST_ROUTE = "/api/v2/engineering-tasks"
ENGINEERING_TASK_DETAIL_ROUTE = "/api/v2/engineering-tasks/{task_id}"
ENGINEERING_TASK_EVENTS_ROUTE = "/api/v2/engineering-tasks/{task_id}/events"
ENGINEERING_TASK_COMMAND_LOG_ROUTE = (
    "/api/v2/engineering-tasks/{task_id}/commands/{command_id}/log"
)
ENGINEERING_TASK_DIFF_ROUTE = "/api/v2/engineering-tasks/{task_id}/diff"
ENGINEERING_TASK_PATCH_ROUTE = "/api/v2/engineering-tasks/{task_id}/patch"
ENGINEERING_TASK_RETRY_REQUESTS_ROUTE = (
    "/api/v2/engineering-tasks/{task_id}/retry-requests"
)
ENGINEERING_TASK_DISCARD_REQUESTS_ROUTE = (
    "/api/v2/engineering-tasks/{task_id}/discard-requests"
)
ENGINEERING_TASK_PROMOTE_REQUESTS_ROUTE = (
    "/api/v2/engineering-tasks/{task_id}/promote-requests"
)
ENGINEERING_TASK_WORKER_VALIDATION_REQUESTS_ROUTE = (
    "/api/v2/engineering-tasks/{task_id}/worker-validation-requests"
)
CODING_RUNS_LIST_ROUTE = "/api/v2/coding-runs"
CODING_RUN_DETAIL_ROUTE = "/api/v2/coding-runs/{coding_run_id}"
CODING_RUN_CLEANUP_ROUTE = "/api/v2/coding-runs/{coding_run_id}/cleanup"
LEGACY_PROJECT_ENGINEERING_TASK_REQUESTS_ROUTE = (
    "/api/v2/legacy-projects/{name}/engineering-task-requests"
)
LEGACY_PROJECT_CODING_TASK_REQUESTS_ROUTE = (
    "/api/v2/legacy-projects/{name}/coding-task-requests"
)
LEGACY_PROJECT_ENGINEERING_TASK_PATH_POLICY_COVERAGE_ROUTE = (
    "/api/v2/legacy-projects/{name}/engineering-task-path-policy-coverage"
)


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
    ],
)


def _runtime(request: Request) -> Any:
    """The single running `app.main.AppState` instance, duck-typed `Any` --
    matches `jobs_v2._runtime()`/`infrastructure_v2._runtime()`/
    `projects_legacy_v2._runtime()`."""

    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    return app_state


def _not_found(message: str = "Resource not found") -> APIError:
    return APIError(code="not_found", message=message, status_code=404)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


# ---------------------------------------------------------------------------
# Capabilities / coding-agents
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Engineering Tasks: list / detail / events / command log / diff / patch
# ---------------------------------------------------------------------------


@router.get("/engineering-tasks")
def list_engineering_tasks(
    request: Request,
    response: Response,
    project: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Wraps legacy `GET /engineering-tasks` byte-for-byte: the merged
    native-engineering-task + legacy-coding-run projection."""

    app_state = _runtime(request)
    if limit < 1 or limit > 100:
        raise APIError(
            code="invalid_limit", message="limit 必須介於 1 與 100", status_code=400
        )
    rows: list[dict[str, Any]] = []
    tasks = app_state.db.list_engineering_tasks(project=project, status=status, limit=limit)
    if app_state.config.authorization_mode == "enforce":
        tasks = filter_project_scoped(
            tasks,
            request.state.request_context,
            lambda task: (task.project_id,) if task.project_id else (),
        )
    for task in tasks:
        row = engineering_presentation.engineering_task_to_dict(task)
        approval = app_state.db.get_approval(task.approval_id)
        run = (
            app_state.db.get_coding_run(task.coding_run_id)
            if task.coding_run_id is not None
            else None
        )
        jobs = app_state.db.list_engineering_task_jobs(task.id)
        events = [
            engineering_presentation.engineering_event_to_dict(event)
            for event in app_state.db.list_engineering_task_events(task.id, limit=100)
        ]
        row["presentation"] = engineering_presentation.engineering_presentation(
            app_state,
            task_data=row,
            approval=approval,
            run=run,
            jobs=jobs,
            events=events,
            event_flags=engineering_presentation.engineering_task_presentation_flags(
                app_state, task
            ),
        )
        rows.append(row)
    legacy_runs = app_state.db.list_coding_runs(status=status, project=project, limit=limit)
    if app_state.config.authorization_mode == "enforce":
        project_ids = {
            project_row.name: project_row.id
            for project_row in app_state.db.list_projects()
            if project_row.id
        }
        legacy_runs = filter_project_scoped(
            legacy_runs,
            request.state.request_context,
            lambda run: (project_ids[run.project],)
            if isinstance(run.project, str) and run.project in project_ids
            else (),
        )
    for run in legacy_runs:
        if run.engineering_task_id is not None:
            continue
        row = engineering_presentation.legacy_coding_run_to_engineering_task(run)
        row["presentation"] = engineering_presentation.engineering_presentation(
            app_state,
            task_data=row,
            approval=app_state.db.get_approval(run.approval_id),
            run=run,
            jobs=[],
            events=engineering_presentation.legacy_engineering_events(run),
        )
        rows.append(row)
    rows.sort(key=lambda row: (row.get("created_at") or "", row["id"]), reverse=True)
    _no_store(response)
    return rows[:limit]


@router.get("/engineering-tasks/{task_id}")
def get_engineering_task(task_id: str, request: Request, response: Response) -> dict[str, Any]:
    """Wraps legacy `GET /engineering-tasks/{id}` byte-for-byte."""

    app_state = _runtime(request)
    detail = engineering_presentation.build_engineering_task_detail(app_state, task_id)
    _no_store(response)
    return detail


@router.get("/engineering-tasks/{task_id}/events")
def list_engineering_task_events(
    task_id: str,
    request: Request,
    response: Response,
    after_id: int = 0,
    attempt_number: Optional[int] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Wraps legacy `GET /engineering-tasks/{id}/events` byte-for-byte."""

    app_state = _runtime(request)
    if after_id < 0 or limit < 1 or limit > 200:
        raise APIError(
            code="invalid_pagination", message="event pagination 參數無效", status_code=400
        )
    _no_store(response)
    if task_id.startswith("legacy-coding-run-"):
        events = engineering_presentation.build_engineering_task_detail(app_state, task_id)[
            "events"
        ]
        return events[:limit]
    if app_state.db.get_engineering_task(task_id) is None:
        raise _not_found("engineering task 不存在")
    return [
        engineering_presentation.engineering_event_to_dict(event)
        for event in app_state.db.list_engineering_task_events(
            task_id, after_id=after_id, attempt_number=attempt_number, limit=limit
        )
    ]


@router.get("/engineering-tasks/{task_id}/commands/{command_id}/log")
def get_engineering_task_command_log(
    task_id: str,
    command_id: int,
    request: Request,
    response: Response,
    lines: int = 200,
) -> dict[str, Any]:
    """Wraps legacy `GET /engineering-tasks/{id}/commands/{command_id}/log`
    byte-for-byte."""

    app_state = _runtime(request)
    if lines < 1 or lines > 1000:
        raise APIError(
            code="invalid_lines", message="lines 必須介於 1 與 1000", status_code=400
        )
    command = app_state.db.get_engineering_task_command(task_id, command_id)
    if command is None or command.job_id is None:
        raise _not_found("engineering task command 不存在")
    job = app_state.db.get_job(command.job_id)
    _no_store(response)
    if job is None or job.log_tail is None:
        return {
            "status": engineering_presentation.safe_engineering_status(
                job.status if job else None, VALID_STATUSES | {"unknown"}
            ),
            "live": False,
            "content": None,
            "available": False,
            "redacted": False,
            "withheld": False,
            "truncated": False,
            "connection": engineering_presentation.engineering_runner_connection(
                app_state, command.target_ref
            ),
        }
    preview = redact_engineering_text(job.log_tail, max_chars=65536)
    if preview.get("content"):
        preview["content"] = "\n".join(preview["content"].splitlines()[-lines:])
    return {
        "status": engineering_presentation.safe_engineering_status(
            job.status, VALID_STATUSES | {"unknown"}
        ),
        "live": False,
        "available": not preview["withheld"],
        "connection": engineering_presentation.engineering_runner_connection(
            app_state, command.target_ref
        ),
        **preview,
    }


@router.get("/engineering-tasks/{task_id}/diff")
def get_engineering_task_diff(task_id: str, request: Request, response: Response) -> dict[str, Any]:
    """Wraps legacy `GET /engineering-tasks/{id}/diff` byte-for-byte."""

    app_state = _runtime(request)
    detail = engineering_presentation.build_engineering_task_detail(app_state, task_id)
    _no_store(response)
    if detail.get("status") == "discarded":
        return {
            "available": False,
            "status": "discarded",
            "summary": None,
            "patch": None,
            "truncated": False,
            "redacted": False,
            "withheld": True,
            "max_chars": 65536,
        }
    run_data = detail.get("coding_run")
    run = (
        app_state.db.get_coding_run(run_data["id"])
        if isinstance(run_data, dict) and isinstance(run_data.get("id"), int)
        else None
    )
    preview = engineering_presentation.engineering_result_preview(
        app_state, run, "diff.patch", max_chars=65536
    )
    if not preview.get("available"):
        status = preview.get("reason") or "missing"
    elif preview.get("withheld"):
        status = "withheld"
    else:
        status = "available"
    return {
        "available": status == "available",
        "status": status,
        "summary": engineering_presentation.engineering_diff_summary(preview),
        "patch": preview.get("content") if status == "available" else None,
        "truncated": bool(preview.get("truncated")),
        "redacted": bool(preview.get("redacted")),
        "withheld": bool(preview.get("withheld")),
        "max_chars": 65536,
    }


@router.get("/engineering-tasks/{task_id}/patch")
def download_engineering_task_patch(task_id: str, request: Request) -> Response:
    """Wraps legacy `GET /engineering-tasks/{id}/patch` byte-for-byte,
    including the exact `X-Artifact-Semantics`/`X-Engineering-Patch-Redacted`
    header contract the legacy download-safety pin covers."""

    app_state = _runtime(request)
    try:
        prepared = engineering_presentation.prepare_sanitized_collected_patch(
            app_state, task_id
        )
    except engineering_presentation.EngineeringPatchDownloadError as exc:
        #: `APIError`'s envelope (`api_error_handler`) always answers with a
        #: JSON body under `Cache-Control`-unspecified defaults; unlike the
        #: legacy raw `HTTPException` this v2 route does not attach the extra
        #: no-store/nosniff headers to the *error* response (no header
        #: parameter exists on `APIError`, matching every other v2 route's
        #: error contract) -- only the successful download below carries
        #: them, same as every other sensitive-artifact v2 download.
        raise APIError(
            code="engineering_patch_unavailable",
            message=exc.detail,
            status_code=exc.status_code,
        ) from None
    safe_task_id = prepared["task_id"]
    filename_suffix = ".redacted.patch" if prepared["redacted"] else ".patch"
    return Response(
        content=prepared["payload"],
        media_type="text/x-diff",
        headers={
            "Content-Disposition": (
                f'attachment; filename="engineering-task-{safe_task_id}'
                f'{filename_suffix}"'
            ),
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "X-Artifact-Semantics": "sanitized-collected-patch",
            "X-Engineering-Patch-Redacted": (
                "true" if prepared["redacted"] else "false"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Engineering Tasks: retry / discard / promote / worker-validation requests
# ---------------------------------------------------------------------------


@router.post("/engineering-tasks/{task_id}/promote-requests")
def request_engineering_task_promote(
    task_id: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /engineering-tasks/{id}/promote-request`: creates a
    `kind=engineering_task_promote` Approval only, same 404/400 gating
    (`CodePromotionDisabledError` -> 404, no `engineering_task_backend_v1`
    pre-check -- matches the legacy endpoint exactly)."""

    app_state = _runtime(request)
    if task_id.startswith("legacy-coding-run-"):
        raise APIError(
            code="legacy_coding_run_unsupported",
            message="legacy Coding Run 沒有 immutable task contract，不能 promote",
            status_code=400,
        )
    try:
        approval = request_engineering_task_promote_approval(
            app_state.db,
            task_id,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CodePromotionDisabledError as exc:
        raise _not_found(str(exc)) from exc
    except InvalidCodePromotionRequestError as exc:
        raise APIError(
            code="invalid_code_promotion_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return {"approval": approvals_module.approval_to_dict(approval)}


# ---------------------------------------------------------------------------
# Coding runs: list / detail / cleanup
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Legacy-project scoped request endpoints
# ---------------------------------------------------------------------------


__all__ = [
    "CODING_AGENTS_ROUTE",
    "CODING_RUNS_LIST_ROUTE",
    "CODING_RUN_CLEANUP_ROUTE",
    "CODING_RUN_DETAIL_ROUTE",
    "ENGINEERING_TASK_CAPABILITIES_ROUTE",
    "ENGINEERING_TASK_COMMAND_LOG_ROUTE",
    "ENGINEERING_TASK_DETAIL_ROUTE",
    "ENGINEERING_TASK_DIFF_ROUTE",
    "ENGINEERING_TASK_DISCARD_REQUESTS_ROUTE",
    "ENGINEERING_TASK_EVENTS_ROUTE",
    "ENGINEERING_TASK_LIST_ROUTE",
    "ENGINEERING_TASK_PATCH_ROUTE",
    "ENGINEERING_TASK_PROMOTE_REQUESTS_ROUTE",
    "ENGINEERING_TASK_RETRY_REQUESTS_ROUTE",
    "ENGINEERING_TASK_WORKER_VALIDATION_REQUESTS_ROUTE",
    "LEGACY_PROJECT_CODING_TASK_REQUESTS_ROUTE",
    "LEGACY_PROJECT_ENGINEERING_TASK_PATH_POLICY_COVERAGE_ROUTE",
    "LEGACY_PROJECT_ENGINEERING_TASK_REQUESTS_ROUTE",
    "router",
]
