"""Job / Runtime v2 read and mutation wrappers (DG-UI-UNIFICATION v1 U3).

Thin `/api/v2/jobs` wrappers around the exact legacy `/jobs` surface in
`app/main.py` (`GET /jobs`, `GET /jobs/{id}`, `GET /jobs/{id}/log`, `GET
/jobs/{id}/results[/...]`, `POST /jobs/{id}/cancel`, `POST /jobs/{id}/stop`,
`POST /jobs/{id}/diagnose`, `POST /dispatch`). Jobs are legacy-scope objects:
they stay on the legacy two-step `Approval` (`kind=enqueue`/`kind=stop`)
mechanic -- auto-approve rules and `web_direct_execute` "one step" execution,
decided through the existing `POST /approve/{id}` / `POST /reject/{id}`
surface -- rather than the Product v2 RBAC transactional decision flow used
by `runs_v2.py` / `experiments_v2.py`. Gated only by `api_v2_feature_gate` +
`product_rbac_v2_feature_gate`: no `run_experience_v2`/`experiment_v2` gate,
because these routes wrap the legacy Job/Approval model, not a Product v2
typed contract.

The `Job -> dict` projection is the single shared implementation in
`app.job_projection` (see that module's docstring for why it is not simply
imported from `app.main`). The approval finalization step below
(`_finalize_job_approval`) duplicates `app.main._finalize_approval`'s body
for the same reason -- `app.main` imports this router before any of its own
function definitions exist, so importing back from `app.main` would be a
circular import (see `app/records.py`'s and `app/auto_placement.py`'s
documented "no `import app.main`" convention). It calls only leaf modules
(`app.approvals`, `app.autoapprove`) already safe to import here, and reaches
the one running `AppState` instance the same way `approve()`/
`maybe_auto_approve()` already document as the supported approach: an
untyped `app_state` object passed through by the caller, taken here from
`request.app.state.dispatch_runtime` (set once in `app.main`'s lifespan).
"""

from __future__ import annotations

import logging
import os
import shlex
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse

from app import approvals as approvals_module
from app import autoapprove
from app.approvals import (
    ApprovalNotFoundError,
    ApprovalNotPendingError,
    JobNotFoundError,
    JobNotRunningError,
    ProjectNotFoundError,
    ServerNotFoundError,
    request_enqueue_approval,
    request_stop_approval,
)
from app.audit import append_audit, audit_actor_from_request_context
from app.authorization_enforce import filter_project_scoped
from app.db import Approval
from app.engineering_tasks import redact_engineering_text
from app.identity import RequestContext
from app.job_projection import engineering_protected_job, job_to_dict
from app.jobqueue import (
    DangerousCommandError,
    EngineeringTaskJobCancellationError,
    build_log_tail_command,
    cancel_job,
)
from app.llm import LLMError, diagnose_job_failure, is_llm_available
from app.llm_local import LLMLocalError, diagnose_job_failure_local, is_vllm_available
from app.results import (
    ResultPathError,
    list_result_files,
    local_result_dir,
    resolve_result_file,
)
from dispatch_center.api.errors import APIError
from dispatch_center.api.schemas import JobCreateRequest, StopJobRequest
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)


logger = logging.getLogger(__name__)

JOBS_LIST_ROUTE = "/api/v2/jobs"
JOB_DETAIL_ROUTE = "/api/v2/jobs/{job_id}"
JOB_LOG_ROUTE = "/api/v2/jobs/{job_id}/log"
JOB_RESULTS_ROUTE = "/api/v2/jobs/{job_id}/results"
JOB_RESULT_FILE_ROUTE = "/api/v2/jobs/{job_id}/results/{file_path:path}"
JOB_CANCEL_ROUTE = "/api/v2/jobs/{job_id}/cancel"
JOB_STOP_REQUEST_ROUTE = "/api/v2/jobs/{job_id}/stop-requests"
JOB_DIAGNOSE_ROUTE = "/api/v2/jobs/{job_id}/diagnose"
DISPATCH_REQUESTS_ROUTE = "/api/v2/dispatch-requests"

#: 階段 10（PLAN.md K.1）duplicated from `app.main._VALID_SOURCES` /
#: `_normalize_source` -- trivial closed-vocabulary normalization, not worth
#: a shared-module extraction. Unmarked/illegal values collapse to "api"
#: (not a 400: `source` is a hint field, not a strictly validated input).
_VALID_SOURCES = {"web", "chatgpt", "vllm", "api"}


def _normalize_source(source: Optional[str]) -> str:
    return source if source in _VALID_SOURCES else "api"


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
    ],
)


def _runtime(request: Request) -> Any:
    """The single running `app.main.AppState` instance, duck-typed `Any` --
    matches how `approvals_module.approve()`/`maybe_auto_approve()` already
    accept `app_state` to avoid importing `app.main.AppState`."""

    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    return app_state


def _not_found() -> APIError:
    return APIError(code="not_found", message="Resource not found", status_code=404)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


async def _finalize_job_approval(
    app_state: Any,
    approval: Approval,
    source: str,
    request_context: RequestContext,
) -> dict[str, Any]:
    """Wraps `app.main._finalize_approval` (see its docstring, `app/main.py`
    :10420-10487, for the full K.2/K.3 asymmetric-response contract this must
    match): `source == "web"` and `web_direct_execute` on -> one-step
    `approve()` in this same request; otherwise consult `app.autoapprove`
    rules -> `maybe_auto_approve()`; neither firing leaves the approval
    pending and returns its plain dict, matching the existing shape callers
    already branch on."""

    result: Optional[dict[str, Any]]
    try:
        if source == "web" and app_state.config.web_direct_execute:
            result = await approvals_module.approve(
                app_state.db,
                approval.id,
                ssh_run=app_state.ssh_run,
                audit_path=app_state.config.audit_path,
                server_configs=app_state.server_configs,
                app_state=app_state,
                approved_by="web-direct",
                note="網頁直接執行（提案者＝批准者）",
                request_context=request_context,
            )
        else:
            rules = autoapprove.get_rules(app_state.config.auto_approve_rules_path)
            result = await approvals_module.maybe_auto_approve(
                app_state.db,
                approval,
                source=source,
                rules=rules,
                ssh_run=app_state.ssh_run,
                server_configs=app_state.server_configs,
                app_state=app_state,
                audit_path=app_state.config.audit_path,
                request_context=request_context,
            )
    except (
        ApprovalNotFoundError,
        ApprovalNotPendingError,
        JobNotFoundError,
        ServerNotFoundError,
        ValueError,
    ) as exc:
        raise APIError(
            code="approval_conflict", message=str(exc), status_code=400
        ) from exc

    if result is None:
        return approvals_module.approval_to_dict(approval)

    response: dict[str, Any] = {
        "approval": approvals_module.approval_to_dict(result["approval"]),
        "auto_approved": True,
    }
    if result.get("job") is not None:
        response["job"] = job_to_dict(result["job"], db=app_state.db)
    return response


@router.get("/jobs")
def list_jobs(
    request: Request,
    response: Response,
    status: Optional[str] = None,
    project: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Wraps legacy `GET /jobs` (`app/main.py` :10388-10409) byte-for-byte:
    the same `db.list_jobs()` call, the same enforce-mode per-item project
    filtering, the same `job_to_dict` projection."""

    app_state = _runtime(request)
    jobs = app_state.db.list_jobs(status=status, project=project)
    if app_state.config.authorization_mode == "enforce":
        project_ids = {
            proj.name: proj.id for proj in app_state.db.list_projects() if proj.id
        }
        jobs = filter_project_scoped(
            jobs,
            request.state.request_context,
            lambda job: (project_ids[job.project],)
            if isinstance(job.project, str) and job.project in project_ids
            else (),
        )
    _no_store(response)
    return [job_to_dict(job, db=app_state.db) for job in jobs]


@router.get("/jobs/{job_id}")
def get_job(job_id: int, request: Request, response: Response) -> dict[str, Any]:
    app_state = _runtime(request)
    job = app_state.db.get_job(job_id)
    if job is None:
        raise _not_found()
    _no_store(response)
    return job_to_dict(job, db=app_state.db)


@router.get("/jobs/{job_id}/log")
async def get_job_log(
    job_id: int, request: Request, response: Response, lines: int = 40
) -> dict[str, Any]:
    """Wraps legacy `GET /jobs/{id}/log` (`app/main.py` :10788-10846): the
    same live-SSH-tail-with-fallback semantics."""

    app_state = _runtime(request)
    job = app_state.db.get_job(job_id)
    if job is None:
        raise _not_found()

    log_tail = job.log_tail
    live = False
    owner_runner_matches = (
        not engineering_protected_job(job)
        or app_state._result_collection_server_config(job) is not None
    )
    if job.status == "running" and job.server and owner_runner_matches:
        try:
            result = await app_state.ssh_run(
                job.server, build_log_tail_command(job_id, lines=lines), 15
            )
            log_tail = result.stdout
            live = True
        except Exception as exc:  # noqa: BLE001 - best effort, fall back
            logger.warning(
                "即時抓取任務 %s log 失敗，退回存好的 log_tail: %s", job_id, exc
            )

    _no_store(response)
    if engineering_protected_job(job):
        preview = redact_engineering_text(log_tail or "", max_chars=65_536)
        return {
            "job_id": job_id,
            "status": job.status,
            "live": live,
            "log_tail": (
                preview.get("content")
                if log_tail is not None and not preview.get("withheld")
                else None
            ),
            "redacted": bool(preview.get("redacted")),
            "withheld": bool(preview.get("withheld")),
            "truncated": bool(preview.get("truncated")),
            "connection": (
                "observed"
                if owner_runner_matches
                else "approved_execution_contract_mismatch"
            ),
        }

    return {"job_id": job_id, "status": job.status, "live": live, "log_tail": log_tail}


@router.get("/jobs/{job_id}/results")
def get_job_results(
    job_id: int, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /jobs/{id}/results` (`app/main.py` :10849-10860):
    local filesystem read only, no SSH."""

    app_state = _runtime(request)
    job = app_state.db.get_job(job_id)
    if job is None:
        raise _not_found()
    result_dir = local_result_dir(job_id, app_state.config.local_home_dir)
    _no_store(response)
    return list_result_files(result_dir)


@router.get("/jobs/{job_id}/results/{file_path:path}")
def get_job_result_file(job_id: int, file_path: str, request: Request) -> FileResponse:
    """Wraps legacy `GET /jobs/{id}/results/{file_path}` (`app/main.py`
    :10863-10886): forced attachment + octet-stream + nosniff, strict
    containment via `resolve_result_file()`."""

    app_state = _runtime(request)
    job = app_state.db.get_job(job_id)
    if job is None:
        raise _not_found()
    result_dir = local_result_dir(job_id, app_state.config.local_home_dir)
    try:
        resolved_path = resolve_result_file(result_dir, file_path)
    except ResultPathError as exc:
        raise APIError(
            code="result_file_error", message=exc.reason, status_code=exc.status_code
        ) from exc
    return FileResponse(
        resolved_path,
        media_type="application/octet-stream",
        filename=os.path.basename(resolved_path),
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job_v2(job_id: int, request: Request, response: Response) -> dict[str, Any]:
    """Wraps legacy `POST /jobs/{id}/cancel` (`app/main.py` :10742-10757):
    direct, queued-only, engineering-protected 409."""

    app_state = _runtime(request)
    try:
        ok = cancel_job(
            app_state.db,
            job_id,
            audit_path=app_state.config.audit_path,
            audit_actor=audit_actor_from_request_context(
                request.state.request_context
            ),
        )
    except EngineeringTaskJobCancellationError as exc:
        raise APIError(
            code="engineering_task_protected", message=str(exc), status_code=409
        ) from exc
    if not ok:
        raise APIError(
            code="job_not_cancelable",
            message="任務不存在或不是 queued 狀態，無法取消",
            status_code=400,
        )
    _no_store(response)
    return {"ok": True}


@router.post("/jobs/{job_id}/stop-requests")
async def request_job_stop(
    job_id: int,
    request: Request,
    response: Response,
    body: Optional[StopJobRequest] = None,
) -> dict[str, Any]:
    """Wraps legacy `POST /jobs/{id}/stop` (`app/main.py` :10760-10785):
    creates the `kind=stop` approval, then the same asymmetric
    `_finalize_approval()` contract."""

    app_state = _runtime(request)
    context: RequestContext = request.state.request_context
    source = _normalize_source(body.source if body is not None else None)
    try:
        approval = request_stop_approval(
            app_state.db,
            job_id,
            source=source,
            audit_path=app_state.config.audit_path,
            request_context=context,
        )
    except JobNotFoundError as exc:
        raise _not_found() from exc
    except JobNotRunningError as exc:
        raise APIError(
            code="job_not_running", message=str(exc), status_code=400
        ) from exc
    result = await _finalize_job_approval(app_state, approval, source, context)
    _no_store(response)
    return result


@router.post("/jobs/{job_id}/diagnose")
async def diagnose_job(
    job_id: int, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /jobs/{id}/diagnose` (`app/main.py`
    :11113-11200): explanation/suggestion only, never executes, never edits,
    never reruns."""

    app_state = _runtime(request)
    context: RequestContext = request.state.request_context
    job = app_state.db.get_job(job_id)
    if job is None:
        raise _not_found()
    if engineering_protected_job(job):
        raise APIError(
            code="engineering_task_protected",
            message=(
                "AI 工程任務的安全診斷動作尚未實作；"
                "請使用 Engineering Task 的遮罩進度與日誌"
            ),
            status_code=409,
        )
    if job.status != "failed":
        raise APIError(
            code="job_not_failed", message="只有 failed 狀態的任務可以診斷", status_code=400
        )

    use_anthropic = is_llm_available(app_state.config)
    use_vllm = not use_anthropic and is_vllm_available(app_state.config)
    if not use_anthropic and not use_vllm:
        raise APIError(
            code="diagnose_unavailable",
            message="未設定 ANTHROPIC_API_KEY，也未設定 VLLM_BASE_URL/VLLM_MODEL，診斷不可用",
            status_code=503,
        )

    project_structure: Optional[str] = None
    if job.project:
        server_state = (
            app_state.server_states.get(job.server) if job.server else None
        )
        if server_state is not None and server_state.online:
            try:
                ssh_result = await app_state.ssh_run(
                    job.server,
                    f"find {shlex.quote('projects/' + job.project)} -maxdepth 2 "
                    "-not -path '*/.git*' | head -100",
                    20,
                )
                project_structure = ssh_result.stdout
            except Exception as exc:  # noqa: BLE001 - best effort, skip on failure
                logger.warning(
                    "診斷任務 %s 時抓取專案結構失敗，略過: %s", job_id, exc
                )

    try:
        if use_anthropic:
            diagnosis = await diagnose_job_failure(
                command=job.command,
                exit_code=job.exit_code,
                log_tail=job.log_tail,
                project_structure=project_structure,
                config=app_state.config,
                client=app_state.llm_client,
            )
        else:
            diagnosis = await diagnose_job_failure_local(
                command=job.command,
                exit_code=job.exit_code,
                log_tail=job.log_tail,
                project_structure=project_structure,
                config=app_state.config,
                client=app_state.vllm_client,
            )
    except (LLMError, LLMLocalError) as exc:
        append_audit(
            "diagnose",
            {"job_id": job_id, "success": False, "error": str(exc)},
            result="failed",
            path=app_state.config.audit_path,
            actor=audit_actor_from_request_context(context),
        )
        raise APIError(
            code="diagnose_failed", message=f"診斷失敗：{exc}", status_code=502
        ) from exc

    append_audit(
        "diagnose",
        {"job_id": job_id, "success": True, "diagnosis": diagnosis},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(context),
    )
    _no_store(response)
    return {"job_id": job_id, "diagnosis": diagnosis}


@router.post("/dispatch-requests")
async def request_dispatch(
    body: JobCreateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /dispatch` / `POST /jobs` (`app/main.py`
    :10490-10538, `_create_enqueue_approval`): dangerous commands rejected
    at request time (never build an approval), then the same
    `_finalize_approval()` asymmetric contract."""

    app_state = _runtime(request)
    context: RequestContext = request.state.request_context
    source = _normalize_source(body.source)
    try:
        approval = request_enqueue_approval(
            app_state.db,
            command=body.command,
            type=body.type,
            project=body.project,
            require_tag=body.require_tag,
            pin_server=body.pin_server,
            depends_on=body.depends_on,
            gpus_needed=body.gpus_needed,
            priority=body.priority,
            source=source,
            source_coding_run_id=body.source_coding_run_id,
            local_home_dir=app_state.config.local_home_dir,
            audit_path=app_state.config.audit_path,
            request_context=context,
        )
    except DangerousCommandError as exc:
        raise APIError(
            code="dangerous_command", message=f"指令被拒絕：{exc}", status_code=400
        ) from exc
    except ProjectNotFoundError as exc:
        raise APIError(
            code="project_not_found", message=str(exc), status_code=404
        ) from exc
    except ValueError as exc:
        raise APIError(
            code="job_request_invalid", message=str(exc), status_code=400
        ) from exc
    result = await _finalize_job_approval(app_state, approval, source, context)
    _no_store(response)
    return result


__all__ = [
    "DISPATCH_REQUESTS_ROUTE",
    "JOBS_LIST_ROUTE",
    "JOB_CANCEL_ROUTE",
    "JOB_DETAIL_ROUTE",
    "JOB_DIAGNOSE_ROUTE",
    "JOB_LOG_ROUTE",
    "JOB_RESULTS_ROUTE",
    "JOB_RESULT_FILE_ROUTE",
    "JOB_STOP_REQUEST_ROUTE",
    "router",
]
