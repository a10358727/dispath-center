"""Project (legacy-scope) and dataset v2 wrappers (DG-UI-UNIFICATION v1 U5).

Thin `/api/v2` wrappers around the exact legacy `/projects*` and `/datasets*`
surfaces in `app/main.py` -- modeled on `jobs_v2.py`/`infrastructure_v2.py`
(U3/U4). These stay legacy-scope `project`/`platform`/`dataset` objects, not
a Product v2 typed contract: `git_init`/`project_deploy` still build a
pending `Approval` decided through `POST /approve/{id}`/the v2 generic
decision fan-out, `hub-sync` is still a direct-execute-plus-audit action
(see `app.hub.sync_project_to_hub`'s own docstring for the accepted risk),
and dataset registration/experiment records are still direct DB writes
(documented exceptions, same as legacy). Gated only by `api_v2_feature_gate`
+ `product_rbac_v2_feature_gate`, exactly like `jobs_v2.py`/
`infrastructure_v2.py`.

Several projection helpers below (`_project_to_dict`, `_instance_to_dict`,
`_project_version_to_dict`, `_activity_server_state_summary`,
`_job_activity_summary`, `_dataset_to_dict`, `_record_to_dict`,
`_enforcement_targets_from_resolution`, `_dataset_enforcement_targets`)
duplicate the body of the matching private helper in `app.main`
byte-for-byte at authoring time -- the same documented reason as
`jobs_v2._finalize_job_approval`: `app.main` imports this router before its
own function definitions exist, so importing back from `app.main` would be a
circular import. Each duplicate calls only leaf modules already safe to
import here (`app.activity`, `app.authorization`, `app.datasets`,
`app.hub`). Parity with the legacy endpoints is asserted directly in
`tests/test_projects_legacy_v2_api.py`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response

from app import approvals as approvals_module
from app.activity import (
    ProjectInstanceResolutionError,
    probe_instance,
)
from app.approvals import (
    InvalidGitInitRequestError,
    request_git_init_approval,
)
from app.audit import append_audit, audit_actor_from_request_context
from app.authorization import Action, ResourceScope, resolve_dataset_resource
from app.authorization_enforce import (
    EnforcementTarget,
    filter_project_scoped,
    filter_targets,
)
from app.datasets import (
    InvalidDatasetCardError,
    InvalidNameError,
    build_dataset_auto_facts,
    build_dataset_card,
    build_manifest,
    render_dataset_card,
    validate_card_fields,
    validate_name_component,
    NO_CARD_NOTE,
)
from app.db import (
    Database,
    Dataset,
    ExperimentRecord,
    Project,
    ProjectInstance,
    ProjectVersion,
    VALID_RECORD_KINDS,
)
from app.hub import (
    HubSyncError,
    InvalidProjectDeployRequestError,
    get_project_hub_info,
    request_project_deploy_approval,
    sync_project_to_hub,
)
from app.job_projection import engineering_job_log_preview
from app.localrun import local_run
from app.monitor import ServerState
from app.records import build_timeline
from dispatch_center.api.errors import APIError
from dispatch_center.api.schemas import (
    DatasetCardUpdateRequest,
    DatasetCreateRequest,
    ExperimentRecordCreateRequest,
    ExperimentRecordPatchRequest,
    GitInitRequest,
    HubSyncRequest,
    ProjectCreateRequest,
    ProjectDeployRequest,
    ProjectPatchRequest,
)
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)


logger = logging.getLogger(__name__)

LEGACY_PROJECTS_LIST_ROUTE = "/api/v2/legacy-projects"
PROJECTS_MATRIX_ROUTE = "/api/v2/projects-matrix"
LEGACY_PROJECT_DETAIL_ROUTE = "/api/v2/legacy-projects/{name}/detail"
LEGACY_PROJECT_VERSIONS_ROUTE = "/api/v2/legacy-projects/{name}/versions"
LEGACY_PROJECT_TIMELINE_ROUTE = "/api/v2/legacy-projects/{name}/timeline"
LEGACY_PROJECT_ACTIVITY_ROUTE = "/api/v2/legacy-projects/{name}/activity"
LEGACY_PROJECT_DETAIL_PATCH_ROUTE = "/api/v2/legacy-projects/{name}"
LEGACY_PROJECT_DELETE_ROUTE = "/api/v2/legacy-projects/{name}"
LEGACY_PROJECT_RECORDS_ROUTE = "/api/v2/legacy-projects/{name}/records"
LEGACY_PROJECT_RECORD_DETAIL_ROUTE = "/api/v2/legacy-projects/{name}/records/{record_id}"
LEGACY_PROJECT_GIT_INIT_REQUESTS_ROUTE = "/api/v2/legacy-projects/{name}/git-init-requests"
LEGACY_PROJECT_HUB_SYNC_ROUTE = "/api/v2/legacy-projects/{name}/hub-sync"
LEGACY_PROJECT_DEPLOY_REQUESTS_ROUTE = "/api/v2/legacy-projects/{name}/deploy-requests"
LEGACY_DATASETS_LIST_ROUTE = "/api/v2/legacy-datasets"
LEGACY_DATASET_CARD_ROUTE = "/api/v2/legacy-datasets/{name}/{version}/card"


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
    ],
)


def _runtime(request: Request) -> Any:
    """The single running `app.main.AppState` instance, duck-typed `Any` --
    matches `jobs_v2._runtime()`/`infrastructure_v2._runtime()`."""

    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    return app_state


def _not_found(message: str = "Resource not found") -> APIError:
    return APIError(code="not_found", message=message, status_code=404)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _project_to_dict(project: Project) -> dict[str, Any]:
    """Duplicates `app.main._project_to_dict` (see module docstring)."""

    return {
        "name": project.name,
        "id": project.id,
        "repo_or_path": project.repo_or_path,
        "dataset_name": project.dataset_name,
        "dataset_version": project.dataset_version,
        "default_command": project.default_command,
        "require_tag": project.require_tag,
        "setup_cmd": project.setup_cmd,
        "created_at": project.created_at,
        "summary": project.summary,
        "dataset_mode": project.dataset_mode,
        "goal": project.goal,
        "optimization_notes": project.optimization_notes,
        "progress": project.progress,
    }


def _instance_to_dict(i: ProjectInstance) -> dict[str, Any]:
    """Duplicates `app.main._instance_to_dict` (see module docstring)."""

    return {
        "id": i.id,
        "project_name": i.project_name,
        "project_id": i.project_id,
        "server": i.server,
        "path": i.path,
        "git_remote": i.git_remote,
        "git_branch": i.git_branch,
        "git_commit": i.git_commit,
        "dirty": i.dirty,
        "embedded_data_paths": i.embedded_data_paths,
        "last_seen": i.last_seen,
        "state": i.state,
    }


def _project_version_to_dict(v: ProjectVersion) -> dict[str, Any]:
    """Duplicates `app.main._project_version_to_dict` (see module docstring)."""

    return {
        "id": v.id,
        "project_id": v.project_id,
        "project_name": v.project_name,
        "git_commit": v.git_commit,
        "git_ref": v.git_ref,
        "source_instance_id": v.source_instance_id,
        "created_at": v.created_at,
        "metadata": v.metadata,
        "promotion_state": v.promotion_state,
    }


def _activity_server_state_summary(state: Optional[ServerState]) -> dict[str, Any]:
    """Duplicates `app.main._activity_server_state_summary` (see module
    docstring)."""

    if state is None:
        return {"online": False, "gpu_util_max": None, "disk_avail_bytes": None}
    return {
        "online": state.online,
        "gpu_util_max": state.gpu_util_max,
        "disk_avail_bytes": state.disk_avail_bytes,
    }


def _job_activity_summary(job: Any) -> dict[str, Any]:
    """Duplicates `app.main._job_activity_summary` (see module docstring)."""

    duration_seconds: Optional[int] = None
    if job.started_at:
        try:
            start = datetime.fromisoformat(job.started_at)
            end = (
                datetime.fromisoformat(job.finished_at)
                if job.finished_at
                else datetime.now(timezone.utc)
            )
            duration_seconds = max(0, int((end - start).total_seconds()))
        except ValueError:
            duration_seconds = None
    return {
        "id": job.id,
        "status": job.status,
        "exit_code": job.exit_code,
        "duration_seconds": duration_seconds,
        "server": job.server,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


def _dataset_to_dict(dataset: Dataset, full: bool = False) -> dict[str, Any]:
    """Duplicates `app.main._dataset_to_dict` (see module docstring)."""

    d = {
        "name": dataset.name,
        "version": dataset.version,
        "size_bytes": dataset.size_bytes,
        "source_path": dataset.source_path,
        "created_at": dataset.created_at,
        "file_count": dataset.manifest.get("file_count"),
        "card": dataset.card,
        "sync_mode": dataset.sync_mode,
    }
    if full:
        d["manifest"] = dataset.manifest
    return d


def _record_to_dict(record: ExperimentRecord) -> dict[str, Any]:
    """Duplicates `app.main._record_to_dict` (see module docstring)."""

    return {
        "id": record.id,
        "project": record.project,
        "kind": record.kind,
        "title": record.title,
        "content": record.content,
        "author": record.author,
        "job_id": record.job_id,
        "coding_run_id": record.coding_run_id,
        "extra": record.extra,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _enforcement_targets_from_resolution(resolution: Any) -> tuple[EnforcementTarget, ...]:
    """Duplicates `app.main._enforcement_targets_from_resolution` (see module
    docstring)."""

    if resolution.scope is None:
        return ()
    if resolution.scope is ResourceScope.GLOBAL:
        return (EnforcementTarget(ResourceScope.GLOBAL),)
    return tuple(
        EnforcementTarget(ResourceScope.PROJECT, project_id=project_id)
        for project_id in resolution.project_ids
    )


def _dataset_enforcement_targets(
    dataset: Dataset, db: Database
) -> tuple[EnforcementTarget, ...]:
    """Duplicates `app.main._dataset_enforcement_targets` (see module
    docstring)."""

    resolution = resolve_dataset_resource(
        dataset.name, dataset.version, dataset, db.list_projects()
    )
    return _enforcement_targets_from_resolution(resolution)


def _resolve_derived_from(db: Database, derived_from: Any) -> Optional[dict]:
    """Duplicates `app.main._resolve_derived_from` (see module docstring)."""

    if derived_from is None:
        return None
    if db.get_dataset(derived_from.name, derived_from.version) is None:
        raise APIError(
            code="derived_from_not_found",
            message=(
                f"derived_from 指向的資料集 {derived_from.name}@{derived_from.version} "
                "尚未註冊，請先註冊該版本或移除 derived_from"
            ),
            status_code=400,
        )
    return {"name": derived_from.name, "version": derived_from.version}


# ---------------------------------------------------------------------------
# Projects: list / create / matrix / detail / versions / timeline / activity
# ---------------------------------------------------------------------------


@router.get("/legacy-projects")
def list_legacy_projects(request: Request, response: Response) -> list[dict[str, Any]]:
    """Wraps legacy `GET /projects` byte-for-byte."""

    app_state = _runtime(request)
    projects = app_state.db.list_projects()
    if app_state.config.authorization_mode == "enforce":
        projects = filter_project_scoped(
            projects,
            request.state.request_context,
            lambda project: (project.id,) if project.id else (),
        )
    _no_store(response)
    return [_project_to_dict(project) for project in projects]


@router.post("/legacy-projects")
def create_legacy_project(
    req: ProjectCreateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /projects`: direct create, no approval (documented
    legacy exception)."""

    app_state = _runtime(request)
    try:
        validate_name_component(req.name, field="name")
        if req.dataset_name:
            validate_name_component(req.dataset_name, field="dataset_name")
        if req.dataset_version:
            validate_name_component(req.dataset_version, field="dataset_version")
    except InvalidNameError as exc:
        raise APIError(code="invalid_name", message=str(exc), status_code=400) from exc

    if bool(req.dataset_name) != bool(req.dataset_version):
        raise APIError(
            code="dataset_name_version_mismatch",
            message="dataset_name 與 dataset_version 必須同時提供或同時省略",
            status_code=400,
        )

    try:
        app_state.db.insert_project(
            name=req.name,
            repo_or_path=req.repo_or_path,
            dataset_name=req.dataset_name,
            dataset_version=req.dataset_version,
            default_command=req.default_command,
            require_tag=req.require_tag,
            setup_cmd=req.setup_cmd,
            audit_actor=audit_actor_from_request_context(request.state.request_context),
        )
    except Exception as exc:  # noqa: BLE001 - sqlite3.IntegrityError: duplicate name
        raise APIError(
            code="project_create_failed",
            message=f"專案 {req.name} 已存在或建立失敗：{exc}",
            status_code=400,
        ) from exc

    _no_store(response)
    return _project_to_dict(app_state.db.get_project(req.name))


@router.get("/projects-matrix")
async def get_legacy_projects_matrix(request: Request, response: Response) -> dict[str, Any]:
    """Wraps legacy `GET /projects/matrix` byte-for-byte: read-only, no
    live SSH."""

    app_state = _runtime(request)
    servers = list(app_state.server_configs.keys())

    projects: list[dict[str, Any]] = []
    for project in app_state.db.list_projects():
        instances = {
            inst.server: {
                "path": inst.path,
                "git_remote": inst.git_remote,
                "git_branch": inst.git_branch,
                "git_commit": inst.git_commit,
                "dirty": inst.dirty,
                "state": inst.state,
            }
            for inst in app_state.db.list_project_instances(project.name)
        }
        hub_info = await get_project_hub_info(
            project.name, app_state.config.local_home_dir, local_run=local_run
        )
        projects.append(
            {
                "name": project.name,
                "repo_or_path": project.repo_or_path,
                "instances": instances,
                "hub": hub_info,
            }
        )

    pending_candidates = {
        server_name: len(
            app_state.db.list_project_candidates(server=server_name, status="pending")
        )
        for server_name in servers
    }

    _no_store(response)
    return {
        "servers": servers,
        "projects": projects,
        "pending_candidates": pending_candidates,
    }


@router.get("/legacy-projects/{name}/detail")
async def get_legacy_project_detail(
    name: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /projects/{name}/detail`: pure DB/local queries, no
    SSH (deliberately not the `/activity` load path -- see legacy
    docstring)."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    instances = app_state.db.list_project_instances(name)
    server_names = sorted({i.server for i in instances})
    server_states_summary = {
        s: _activity_server_state_summary(app_state.server_states.get(s))
        for s in server_names
    }
    hub_info = await get_project_hub_info(
        name, app_state.config.local_home_dir, local_run=local_run
    )
    versions = app_state.db.list_project_versions(name)

    _no_store(response)
    return {
        "project": _project_to_dict(project),
        "instances": [_instance_to_dict(i) for i in instances],
        "server_states": server_states_summary,
        "hub": hub_info,
        "versions": [_project_version_to_dict(v) for v in versions],
    }


@router.get("/legacy-projects/{name}/versions")
def get_legacy_project_versions(
    name: str, request: Request, response: Response
) -> list[dict[str, Any]]:
    """Wraps legacy `GET /projects/{name}/versions`: empty list (not 404)
    for an unknown project, matching legacy behavior."""

    app_state = _runtime(request)
    _no_store(response)
    return [
        _project_version_to_dict(v) for v in app_state.db.list_project_versions(name)
    ]


@router.get("/legacy-projects/{name}/timeline")
def get_legacy_project_timeline(
    name: str,
    request: Request,
    response: Response,
    q: Optional[str] = None,
    limit: int = 20,
    before_ts: Optional[str] = None,
    kinds: Optional[str] = None,
) -> dict[str, Any]:
    """Wraps legacy `GET /projects/{name}/timeline`."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    kinds_list = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    _no_store(response)
    return build_timeline(
        app_state.db, name, q=q, limit=limit, before_ts=before_ts, kinds=kinds_list
    )


@router.get("/legacy-projects/{name}/activity")
async def get_legacy_project_activity(
    name: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /projects/{name}/activity`: live, read-only, direct
    (no approval) per-instance SSH probe, same as the legacy endpoint --
    writes audit `project_activity` on every call."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    instances = app_state.db.list_project_instances(name)

    jobs = app_state.db.list_jobs(project=name)
    recent_jobs = [_job_activity_summary(j) for j in jobs[-10:]]
    latest_job_log_tail = None
    if jobs:
        latest = jobs[-1]
        if latest.engineering_task_id is None:
            latest_job_log_tail = latest.log_tail
        else:
            preview = engineering_job_log_preview(latest, max_chars=12_000)
            latest_job_log_tail = (
                preview.get("content") if not preview.get("withheld") else None
            )

    server_names = sorted({i.server for i in instances})
    server_states_summary = {
        s: _activity_server_state_summary(app_state.server_states.get(s))
        for s in server_names
    }

    activity: Any
    probed_servers: list[str] = []
    if not instances:
        activity = (
            f"專案 {name} 沒有已登記的機器/路徑（project_instances 為空），"
            "無法探測執行近況——請先透過 inventory scan／匯入流程登記至少一台機器。"
        )
    else:
        activity = []
        for inst in instances:
            state = app_state.server_states.get(inst.server)
            if state is None or not state.online:
                activity.append(
                    {
                        "instance_id": inst.id,
                        "server": inst.server,
                        "path": inst.path,
                        "skipped": "offline",
                    }
                )
                continue
            server_cfg = app_state.server_configs.get(inst.server)
            exclude_names = server_cfg.project_exclude_names if server_cfg else None
            probe_result = await probe_instance(
                app_state.ssh_run, inst.server, inst.path, exclude_names
            )
            activity.append(
                {
                    "instance_id": inst.id,
                    "server": inst.server,
                    "path": inst.path,
                    **probe_result,
                }
            )
            probed_servers.append(inst.server)

    append_audit(
        "project_activity",
        {"project": name, "probed": probed_servers},
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )

    _no_store(response)
    return {
        "project": _project_to_dict(project),
        "instances": [_instance_to_dict(i) for i in instances],
        "server_states": server_states_summary,
        "recent_jobs": recent_jobs,
        "latest_job_log_tail": latest_job_log_tail,
        "activity": activity,
    }


@router.patch("/legacy-projects/{name}")
def patch_legacy_project(
    name: str, req: ProjectPatchRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `PATCH /projects/{name}`: `exclude_unset` partial
    update, empty body -> 400."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise APIError(
            code="empty_patch", message="body 至少要帶一個欄位", status_code=400
        )

    app_state.db.update_project(
        name,
        audit_actor=audit_actor_from_request_context(request.state.request_context),
        **fields,
    )
    _no_store(response)
    return _project_to_dict(app_state.db.get_project(name))


@router.delete("/legacy-projects/{name}")
def delete_legacy_project(
    name: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `DELETE /projects/{name}`: DB-only, no SSH; 409 with
    queued/running jobs referencing this project."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    blocking_job_ids = sorted(
        j.id
        for j in app_state.db.list_jobs(status="queued") + app_state.db.list_jobs(status="running")
        if j.project == name
    )
    if blocking_job_ids:
        raise APIError(
            code="project_has_blocking_jobs",
            message=f"專案 {name} 仍有 queued/running 任務引用（{blocking_job_ids}），無法刪除",
            status_code=409,
        )

    app_state.db.delete_project(
        name, audit_actor=audit_actor_from_request_context(request.state.request_context)
    )
    _no_store(response)
    return {"ok": True, "name": name}


# ---------------------------------------------------------------------------
# Experiment records: direct CRUD (documented legacy exception)
# ---------------------------------------------------------------------------


@router.post("/legacy-projects/{name}/records")
def create_legacy_experiment_record(
    name: str,
    req: ExperimentRecordCreateRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/records`: direct DB write, no
    approval (documented legacy exception)."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")
    if req.kind not in VALID_RECORD_KINDS:
        raise APIError(
            code="invalid_record_kind",
            message=f"kind 必須是 {sorted(VALID_RECORD_KINDS)} 其中之一",
            status_code=400,
        )
    author = req.author or "user"
    if author != "user" and not author.startswith("agent"):
        raise APIError(
            code="invalid_record_author",
            message="author 必須是 user 或以 agent 開頭",
            status_code=400,
        )

    record_id = app_state.db.insert_experiment_record(
        project=name,
        content=req.content,
        kind=req.kind,
        title=req.title,
        author=author,
        job_id=req.job_id,
        coding_run_id=req.coding_run_id,
        audit_actor=audit_actor_from_request_context(request.state.request_context),
    )
    _no_store(response)
    return _record_to_dict(app_state.db.get_experiment_record(record_id))


@router.patch("/legacy-projects/{name}/records/{record_id}")
def patch_legacy_experiment_record(
    name: str,
    record_id: int,
    req: ExperimentRecordPatchRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Wraps legacy `PATCH /projects/{name}/records/{id}`."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    record = app_state.db.get_experiment_record(record_id)
    if record is None or record.project != name:
        raise _not_found(f"紀錄 {record_id} 不存在")

    fields = req.model_dump(exclude_unset=True)
    if not fields:
        raise APIError(
            code="empty_patch", message="body 至少要帶一個欄位", status_code=400
        )
    if "kind" in fields and fields["kind"] not in VALID_RECORD_KINDS:
        raise APIError(
            code="invalid_record_kind",
            message=f"kind 必須是 {sorted(VALID_RECORD_KINDS)} 其中之一",
            status_code=400,
        )

    app_state.db.update_experiment_record(
        record_id,
        audit_actor=audit_actor_from_request_context(request.state.request_context),
        **fields,
    )
    _no_store(response)
    return _record_to_dict(app_state.db.get_experiment_record(record_id))


@router.delete("/legacy-projects/{name}/records/{record_id}")
def delete_legacy_experiment_record(
    name: str, record_id: int, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `DELETE /projects/{name}/records/{id}`."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")

    record = app_state.db.get_experiment_record(record_id)
    if record is None or record.project != name:
        raise _not_found(f"紀錄 {record_id} 不存在")

    app_state.db.delete_experiment_record(
        record_id,
        audit_actor=audit_actor_from_request_context(request.state.request_context),
    )
    _no_store(response)
    return {"ok": True, "id": record_id}


# ---------------------------------------------------------------------------
# git-init / hub-sync / deploy
# ---------------------------------------------------------------------------


@router.post("/legacy-projects/{name}/git-init-requests")
async def request_legacy_git_init(
    name: str, req: GitInitRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/git-init-request`: creates a
    `kind=git_init` Approval only."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")
    try:
        approval = await request_git_init_approval(
            app_state.db,
            name,
            req.server,
            req.extra_ignores,
            ssh_run=app_state.ssh_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidGitInitRequestError as exc:
        raise APIError(
            code="invalid_git_init_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/legacy-projects/{name}/hub-sync")
async def legacy_hub_sync(
    name: str, req: HubSyncRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/hub-sync`: direct execute + audit,
    no approval card (see `app.hub.sync_project_to_hub` docstring for the
    accepted risk)."""

    app_state = _runtime(request)
    try:
        result = await sync_project_to_hub(
            app_state.db,
            name,
            req.server,
            ssh_run=app_state.ssh_run,
            local_run=local_run,
            config=app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ProjectInstanceResolutionError as exc:
        raise _not_found(str(exc)) from exc
    except HubSyncError as exc:
        raise APIError(code="hub_sync_failed", message=str(exc), status_code=400) from exc
    _no_store(response)
    return result


@router.post("/legacy-projects/{name}/deploy-requests")
async def request_legacy_project_deploy(
    name: str, req: ProjectDeployRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /projects/{name}/deploy-request`: creates a
    `kind=project_deploy` Approval only."""

    app_state = _runtime(request)
    project = app_state.db.get_project(name)
    if project is None:
        raise _not_found(f"專案 {name} 不存在")
    try:
        approval = await request_project_deploy_approval(
            app_state.db,
            name,
            req.target_server,
            req.dest_path,
            req.ref,
            config=app_state.config,
            server_configs=app_state.server_configs,
            ssh_run=app_state.ssh_run,
            local_run=local_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except InvalidProjectDeployRequestError as exc:
        raise APIError(
            code="invalid_project_deploy_request", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


# ---------------------------------------------------------------------------
# Datasets: list / create / card
# ---------------------------------------------------------------------------


@router.get("/legacy-datasets")
def list_legacy_datasets(request: Request, response: Response) -> list[dict[str, Any]]:
    """Wraps legacy `GET /datasets`."""

    app_state = _runtime(request)
    datasets = app_state.db.list_datasets()
    if app_state.config.authorization_mode == "enforce":
        datasets = filter_targets(
            datasets,
            request.state.request_context,
            lambda dataset: _dataset_enforcement_targets(dataset, app_state.db),
            action=Action.PROJECT_VIEW,
        )
    _no_store(response)
    return [_dataset_to_dict(dataset) for dataset in datasets]


@router.post("/legacy-datasets")
async def create_legacy_dataset(
    req: DatasetCreateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /datasets`: direct create, no approval (documented
    legacy exception); `description`/`method` mandatory."""

    app_state = _runtime(request)
    try:
        validate_name_component(req.name, field="name")
        validate_name_component(req.version, field="version")
    except InvalidNameError as exc:
        raise APIError(code="invalid_name", message=str(exc), status_code=400) from exc

    try:
        validate_card_fields(req.description, req.method)
    except InvalidDatasetCardError as exc:
        raise APIError(code="invalid_dataset_card", message=str(exc), status_code=400) from exc

    derived_from_dict = _resolve_derived_from(app_state.db, req.derived_from)

    try:
        card = build_dataset_card(req.description, req.method, derived_from_dict, req.counts)
    except InvalidDatasetCardError as exc:
        raise APIError(code="invalid_dataset_card", message=str(exc), status_code=400) from exc

    try:
        manifest = await asyncio.to_thread(build_manifest, req.source_path)
    except ValueError as exc:
        raise APIError(code="invalid_source_path", message=str(exc), status_code=400) from exc

    try:
        app_state.db.insert_dataset(
            name=req.name,
            version=req.version,
            size_bytes=manifest["total_size"],
            source_path=req.source_path,
            manifest=manifest,
            card=card,
            audit_actor=audit_actor_from_request_context(request.state.request_context),
        )
    except Exception as exc:  # noqa: BLE001 - sqlite3.IntegrityError: duplicate (name, version)
        raise APIError(
            code="dataset_create_failed",
            message=f"資料集 {req.name}@{req.version} 已註冊過或建立失敗：{exc}",
            status_code=400,
        ) from exc

    _no_store(response)
    return _dataset_to_dict(app_state.db.get_dataset(req.name, req.version), full=True)


@router.get("/legacy-datasets/{name}/{version}/card")
def get_legacy_dataset_card(
    name: str, version: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /datasets/{name}/{version}/card`."""

    app_state = _runtime(request)
    dataset = app_state.db.get_dataset(name, version)
    if dataset is None:
        raise _not_found(f"資料集 {name}@{version} 不存在")

    auto_facts = build_dataset_auto_facts(app_state.db, dataset)
    rendered = render_dataset_card(name, version, dataset.card, auto_facts)
    note = None if dataset.card is not None else NO_CARD_NOTE
    _no_store(response)
    return {
        "name": name,
        "version": version,
        "card": dataset.card,
        "auto_facts": auto_facts,
        "rendered": rendered,
        "note": note,
    }


@router.patch("/legacy-datasets/{name}/{version}/card")
def patch_legacy_dataset_card(
    name: str,
    version: str,
    req: DatasetCardUpdateRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Wraps legacy `PATCH /datasets/{name}/{version}/card`."""

    app_state = _runtime(request)
    dataset = app_state.db.get_dataset(name, version)
    if dataset is None:
        raise _not_found(f"資料集 {name}@{version} 不存在")

    try:
        validate_card_fields(req.description, req.method)
    except InvalidDatasetCardError as exc:
        raise APIError(code="invalid_dataset_card", message=str(exc), status_code=400) from exc

    derived_from_dict = _resolve_derived_from(app_state.db, req.derived_from)

    try:
        new_card = build_dataset_card(req.description, req.method, derived_from_dict, req.counts)
    except InvalidDatasetCardError as exc:
        raise APIError(code="invalid_dataset_card", message=str(exc), status_code=400) from exc

    if dataset.card and dataset.card.get("created_at"):
        new_card["created_at"] = dataset.card["created_at"]

    app_state.db.update_dataset_card(
        name,
        version,
        new_card,
        audit_actor=audit_actor_from_request_context(request.state.request_context),
    )

    updated = app_state.db.get_dataset(name, version)
    auto_facts = build_dataset_auto_facts(app_state.db, updated)
    rendered = render_dataset_card(name, version, updated.card, auto_facts)
    _no_store(response)
    return {
        "name": name,
        "version": version,
        "card": updated.card,
        "auto_facts": auto_facts,
        "rendered": rendered,
        "note": None,
    }


__all__ = [
    "LEGACY_DATASETS_LIST_ROUTE",
    "LEGACY_DATASET_CARD_ROUTE",
    "LEGACY_PROJECTS_LIST_ROUTE",
    "LEGACY_PROJECT_ACTIVITY_ROUTE",
    "LEGACY_PROJECT_DELETE_ROUTE",
    "LEGACY_PROJECT_DEPLOY_REQUESTS_ROUTE",
    "LEGACY_PROJECT_DETAIL_PATCH_ROUTE",
    "LEGACY_PROJECT_DETAIL_ROUTE",
    "LEGACY_PROJECT_GIT_INIT_REQUESTS_ROUTE",
    "LEGACY_PROJECT_HUB_SYNC_ROUTE",
    "LEGACY_PROJECT_RECORDS_ROUTE",
    "LEGACY_PROJECT_RECORD_DETAIL_ROUTE",
    "LEGACY_PROJECT_TIMELINE_ROUTE",
    "LEGACY_PROJECT_VERSIONS_ROUTE",
    "PROJECTS_MATRIX_ROUTE",
    "router",
]
