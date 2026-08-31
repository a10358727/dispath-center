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
    CodingRunNotCleanableError,
    CodingRunNotFoundError,
    InvalidCodePromotionRequestError,
    InvalidCodingTaskRequestError,
    InvalidEngineeringValidationRequestError,
    request_engineering_task_discard_approval,
    request_engineering_task_promote_approval,
    request_engineering_task_retry_approval,
)
from app.authorization_enforce import filter_project_scoped
from app.coding_agents import (
    list_coding_agent_runtime_capability_snapshots,
)
from app.engineering_tasks import (
    InvalidEngineeringTaskRequestError,
    preview_hub_path_policy_coverage,
    redact_engineering_text,
)
from app.jobqueue import DangerousCommandError
from app.localrun import local_run
from app.db import VALID_STATUSES
from dispatch_center.api.errors import APIError
from dispatch_center.api.schemas import (
    CodingTaskRequest,
    EngineeringTaskCreateRequest,
    EngineeringTaskPathPolicyCoverageRequest,
    EngineeringWorkerValidationRequest,
)
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


def _engineering_task_backend_disabled() -> APIError:
    return APIError(
        code="engineering_task_backend_disabled",
        message="AI Engineering Task backend 未啟用",
        status_code=404,
    )


def _selectable_coding_agent_capability_snapshots(app_state: Any) -> list[dict]:
    """Duplicates `app.main._selectable_coding_agent_capability_snapshots`
    (see that docstring): Phase 1b retired every exec adapter, so nothing is
    selectable for a new request."""

    del app_state
    return []


# ---------------------------------------------------------------------------
# Capabilities / coding-agents
# ---------------------------------------------------------------------------


@router.get("/engineering-tasks/capabilities")
def get_engineering_task_capabilities(
    request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /engineering-tasks/capabilities` byte-for-byte."""

    app_state = _runtime(request)
    _no_store(response)
    return {
        "enabled": app_state.config.engineering_task_backend_v1,
        "contract_version": (
            "engineering-task-v2"
            if app_state.config.engineering_task_backend_v1
            else None
        ),
        "providers": _selectable_coding_agent_capability_snapshots(app_state),
    }


@router.get("/coding-agents")
def get_coding_agents(request: Request, response: Response) -> dict[str, Any]:
    """Wraps legacy `GET /coding-agents` byte-for-byte."""

    _runtime(request)
    providers = list_coding_agent_runtime_capability_snapshots()
    _no_store(response)
    return {"providers": providers}


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


@router.post("/engineering-tasks/{task_id}/retry-requests")
def request_engineering_task_retry(
    task_id: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /engineering-tasks/{id}/retry-request`: creates a
    `kind=engineering_task_retry` Approval only, same 404/400 gating."""

    app_state = _runtime(request)
    if not app_state.config.engineering_task_backend_v1:
        raise _engineering_task_backend_disabled()
    if task_id.startswith("legacy-coding-run-"):
        raise APIError(
            code="legacy_coding_run_unsupported",
            message="legacy Coding Run 沒有 immutable task contract，不能 retry",
            status_code=400,
        )
    try:
        approval = request_engineering_task_retry_approval(
            app_state.db,
            task_id,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidEngineeringTaskRequestError as exc:
        raise APIError(
            code="invalid_engineering_task_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return {"approval": approvals_module.approval_to_dict(approval)}


@router.post("/engineering-tasks/{task_id}/discard-requests")
def request_engineering_task_discard(
    task_id: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /engineering-tasks/{id}/discard-request`: creates a
    `kind=engineering_task_discard` Approval only, same 404/400 gating."""

    app_state = _runtime(request)
    if not app_state.config.engineering_task_backend_v1:
        raise _engineering_task_backend_disabled()
    if task_id.startswith("legacy-coding-run-"):
        raise APIError(
            code="legacy_coding_run_unsupported",
            message="legacy Coding Run 沒有 immutable task contract，不能 discard",
            status_code=400,
        )
    try:
        approval = request_engineering_task_discard_approval(
            app_state.db,
            task_id,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidEngineeringTaskRequestError as exc:
        raise APIError(
            code="invalid_engineering_task_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return {"approval": approvals_module.approval_to_dict(approval)}


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


@router.post("/engineering-tasks/{task_id}/worker-validation-requests")
def request_engineering_worker_validation(
    task_id: str,
    req: EngineeringWorkerValidationRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Wraps legacy `POST /engineering-tasks/{id}/worker-validation-request`:
    creates a pending enqueue approval without creating a Job or using SSH."""

    app_state = _runtime(request)
    if not app_state.config.engineering_task_backend_v1:
        raise _engineering_task_backend_disabled()
    if task_id.startswith("legacy-coding-run-"):
        raise APIError(
            code="legacy_coding_run_unsupported",
            message="legacy Coding Run 沒有 immutable task contract，不能提出 worker validation",
            status_code=400,
        )
    try:
        validation, approval = (
            approvals_module.request_engineering_worker_validation_approval(
                app_state.db,
                task_id,
                command=req.command,
                pin_server=req.pin_server,
                server_configs=app_state.server_configs,
                local_home_dir=app_state.config.local_home_dir,
                gpus_needed=req.gpus_needed,
                priority=req.priority,
                require_tag=req.require_tag,
                audit_path=app_state.config.audit_path,
                request_context=request.state.request_context,
            )
        )
    except (InvalidEngineeringValidationRequestError, DangerousCommandError) as exc:
        raise APIError(
            code="invalid_worker_validation_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return {
        "validation_request": engineering_presentation.engineering_validation_request_to_dict(
            app_state, validation
        ),
        "approval": approvals_module.approval_to_dict(approval),
    }


# ---------------------------------------------------------------------------
# Coding runs: list / detail / cleanup
# ---------------------------------------------------------------------------


@router.get("/coding-runs")
def list_coding_runs(
    request: Request,
    response: Response,
    status: Optional[str] = None,
    project: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Wraps legacy `GET /coding-runs` byte-for-byte."""

    app_state = _runtime(request)
    runs = app_state.db.list_coding_runs(status=status, project=project, limit=limit)
    if app_state.config.authorization_mode == "enforce":
        project_ids = {
            project_row.name: project_row.id
            for project_row in app_state.db.list_projects()
            if project_row.id
        }
        runs = filter_project_scoped(
            runs,
            request.state.request_context,
            lambda run: (project_ids[run.project],)
            if isinstance(run.project, str) and run.project in project_ids
            else (),
        )
    _no_store(response)
    return [
        engineering_presentation.engineering_coding_run_to_dict(run)
        if run.engineering_task_id is not None
        else engineering_presentation.coding_run_to_dict(run)
        for run in runs
    ]


@router.get("/coding-runs/{coding_run_id}")
def get_coding_run(coding_run_id: int, request: Request, response: Response) -> dict[str, Any]:
    """Wraps legacy `GET /coding-runs/{id}` byte-for-byte."""

    app_state = _runtime(request)
    run = app_state.db.get_coding_run(coding_run_id)
    if run is None:
        raise _not_found(f"coding_run {coding_run_id} 不存在")
    _no_store(response)
    if run.engineering_task_id is None:
        data = engineering_presentation.coding_run_to_dict(run)
        data["final_message"] = engineering_presentation.read_local_coding_result_file(
            app_state, run.job_id, "final_message.txt"
        )
        data["diff_patch"] = engineering_presentation.read_local_coding_result_file(
            app_state, run.job_id, "diff.patch"
        )
        return data

    data = engineering_presentation.engineering_coding_run_to_dict(run)
    final_preview = engineering_presentation.engineering_result_preview(
        app_state,
        run,
        "final_message.txt",
        max_chars=engineering_presentation.CODING_RUN_FILE_MAX_CHARS,
    )
    diff_preview = engineering_presentation.engineering_result_preview(
        app_state,
        run,
        "diff.patch",
        max_chars=engineering_presentation.CODING_RUN_FILE_MAX_CHARS,
    )
    data.update(
        {
            "final_message": (
                final_preview.get("content")
                if final_preview.get("available") and not final_preview.get("withheld")
                else None
            ),
            "diff_patch": (
                diff_preview.get("content")
                if diff_preview.get("available") and not diff_preview.get("withheld")
                else None
            ),
            "result_visibility": {
                "final_message": {
                    "available": bool(final_preview.get("available")),
                    "redacted": bool(final_preview.get("redacted")),
                    "withheld": bool(final_preview.get("withheld")),
                    "truncated": bool(final_preview.get("truncated")),
                },
                "diff_patch": {
                    "available": bool(diff_preview.get("available")),
                    "redacted": bool(diff_preview.get("redacted")),
                    "withheld": bool(diff_preview.get("withheld")),
                    "truncated": bool(diff_preview.get("truncated")),
                },
            },
        }
    )
    return data


@router.post("/coding-runs/{coding_run_id}/cleanup")
async def cleanup_coding_run_v2(
    coding_run_id: int, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /coding-runs/{id}/cleanup` byte-for-byte (PLAN.md
    N.9 rule 11): web-triggered + audited, never given to MCP."""

    app_state = _runtime(request)
    run = app_state.db.get_coding_run(coding_run_id)
    if run is not None and run.engineering_task_id is not None:
        availability = engineering_presentation.engineering_cleanup_availability(
            app_state, run
        )
        if not availability["enabled"]:
            raise APIError(
                code="coding_run_not_cleanable",
                message=availability["reason"],
                status_code=409,
            )
    try:
        result = await approvals_module.cleanup_coding_run(
            app_state.db,
            coding_run_id,
            ssh_run=app_state.ssh_run,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CodingRunNotFoundError as exc:
        raise _not_found(str(exc)) from exc
    except CodingRunNotCleanableError as exc:
        raise APIError(
            code="coding_run_not_cleanable", message=str(exc), status_code=409
        ) from exc
    _no_store(response)
    return result


# ---------------------------------------------------------------------------
# Legacy-project scoped request endpoints
# ---------------------------------------------------------------------------


@router.post("/legacy-projects/{name}/engineering-task-requests")
async def request_legacy_project_engineering_task(
    name: str, req: EngineeringTaskCreateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/engineering-tasks/request`: builds
    an immutable ProjectVersion-pinned AI Engineering Task approval, same
    Runner-availability preflight (503) and 404/400 gating."""

    app_state = _runtime(request)
    if not app_state.config.engineering_task_backend_v1:
        raise _engineering_task_backend_disabled()
    if app_state.db.get_project(name) is None:
        raise _not_found(f"專案 {name} 不存在")

    runner_status = await app_state.get_codex_runner_status()
    unavailable_reason = None
    if not runner_status.get("configured"):
        unavailable_reason = "Coding Runner 尚未設定"
    elif not runner_status.get("online"):
        unavailable_reason = "Coding Runner 離線"
    elif runner_status.get("probe_status") == "probe_failed":
        unavailable_reason = "Coding Runner 能力探測失敗"
    elif not runner_status.get("codex_installed"):
        unavailable_reason = "Coding Runner 未安裝 Codex"
    elif not runner_status.get("authenticated"):
        unavailable_reason = "Coding Runner 尚未完成 Codex login"
    if unavailable_reason:
        raise APIError(
            code="codex_runner_unavailable", message=unavailable_reason, status_code=503
        )

    structured = req.model_dump()
    structured["permissions"] = structured.pop("execution_permissions")
    try:
        task, approval = await approvals_module.request_engineering_task_approval(
            app_state.db,
            name,
            project_version_id=req.project_version_id,
            agent_provider_id=req.agent_provider_id,
            structured_request=structured,
            config=app_state.config,
            server_enabled={
                server.name: server.enabled for server in app_state.server_configs.values()
            },
            local_run=local_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidEngineeringTaskRequestError as exc:
        raise APIError(
            code="invalid_engineering_task_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return {
        "task": engineering_presentation.engineering_task_to_dict(task),
        "approval": approvals_module.approval_to_dict(approval),
    }


@router.post("/legacy-projects/{name}/coding-task-requests")
async def request_legacy_project_coding_task(
    name: str, req: CodingTaskRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/coding-task-request`: creates a
    `kind=coding_task` Approval only, no dispatch."""

    app_state = _runtime(request)
    try:
        approval = approvals_module.request_coding_task_approval(
            app_state.db,
            name,
            req.instruction,
            base_branch=req.base_branch,
            validation_target=req.validation_target,
            config=app_state.config,
            server_enabled={s.name: s.enabled for s in app_state.server_configs.values()},
            legacy_server=req.server,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidCodingTaskRequestError as exc:
        raise APIError(
            code="invalid_coding_task_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/legacy-projects/{name}/engineering-task-path-policy-coverage")
async def request_legacy_project_path_policy_coverage(
    name: str,
    req: EngineeringTaskPathPolicyCoverageRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/engineering-tasks/path-policy-
    coverage`: purely advisory, read-only, never creates an approval or
    record."""

    app_state = _runtime(request)
    if not app_state.config.engineering_task_backend_v1:
        raise _engineering_task_backend_disabled()
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")
    version = app_state.db.get_project_version(req.project_version_id)
    if (
        version is None
        or version.project_name != name
        or version.project_id != project.id
    ):
        raise APIError(
            code="project_version_mismatch",
            message="ProjectVersion 不屬於目前這個 Project",
            status_code=400,
        )

    try:
        coverage = await preview_hub_path_policy_coverage(
            project_name=name,
            git_commit=version.git_commit,
            allowed_paths=req.allowed_paths,
            prohibited_paths=req.prohibited_paths,
            local_home_dir=app_state.config.local_home_dir,
            local_run=local_run,
        )
    except InvalidEngineeringTaskRequestError as exc:
        raise APIError(
            code="invalid_engineering_task_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return coverage


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
