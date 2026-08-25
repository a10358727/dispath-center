"""Infrastructure (workers / server-config / inventory / codex runner) v2
wrappers (DG-UI-UNIFICATION v1 U4).

Thin `/api/v2` wrappers around the exact legacy `/servers*`, `/server-config*`,
`/inventory/*`, and `/codex-runner/status` surfaces in `app/main.py`. Every
mutation route below only *creates a pending Approval* (or, for the manual
candidate endpoint, a `status=pending` `ProjectCandidate` row) -- exactly like
its legacy counterpart -- and never writes `servers.yaml`, never SSHes to
provision anything, never bypasses `POST /approve/{id}` / the v2 generic
decision fan-out. Gated by `api_v2_feature_gate` + `product_rbac_v2_feature_gate`
only, the same as `jobs_v2.py`: these are legacy-scope `platform` objects, not
a Product v2 typed contract.

Several projection helpers below (`_server_state_to_dict`,
`_idle_summary_to_dict`, `_candidate_to_dict`,
`_server_config_with_attempt_evidence`) duplicate the body of the matching
private helper in `app.main` byte-for-byte at authoring time. This mirrors
`jobs_v2._finalize_job_approval`'s documented reason: `app.main` imports this
router before its own function definitions exist, so importing back from
`app.main` would be a circular import (see `app/records.py`'s and
`app/auto_placement.py`'s "no `import app.main`" convention). Each duplicate
calls only leaf modules already safe to import here (`app.server_config`,
`app.capacity`, `app.node_protocol`) or reaches the one running `AppState`
instance the same duck-typed way `jobs_v2._runtime()` already does, including
its private `_matching_active_server_revision` helper (matching how
`jobs_v2.get_job_log` already reaches
`app_state._result_collection_server_config`). Parity with the legacy
endpoints is asserted directly in `tests/test_infrastructure_v2_api.py`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request, Response

from app import approvals as approvals_module
from app.approvals import (
    CandidateNotFoundError,
    CandidateNotPendingError,
    ForbiddenScanRootError,
    InvalidServerConfigError,
    ManualCandidateDuplicateError,
    ManualCandidatePathInvalidError,
    ManualCandidateServerInvalidError,
    NoNestedCandidatesError,
    ServerNotFoundError,
    ServerRenameNotSupportedError,
)
from app.audit import append_audit, audit_actor_from_request_context
from app.capacity import summarize_observations
from app.config import ServerConfig
from app.datasets import InvalidNameError
from app.monitor import ServerState
from app.node_protocol import resolve_execution_backend
from app.server_config import (
    load_servers_config,
    server_config_to_safe_dict,
    test_ssh_connection,
    validate_server_config,
)
from dispatch_center.api.errors import APIError
from dispatch_center.api.schemas import (
    ImportProjectCandidateRequest,
    InventoryScanRequest,
    ManualCandidateRequest,
    ServerConfigPayload,
    ServerNameRequest,
    ServerUpdateRequest,
)
from dispatch_center.api.v2 import (
    API_V2_PREFIX,
    api_v2_feature_gate,
    product_rbac_v2_feature_gate,
)


logger = logging.getLogger(__name__)

SERVERS_LIST_ROUTE = "/api/v2/servers"
SERVERS_IDLE_SUMMARY_ROUTE = "/api/v2/servers/idle-summary"
SERVER_CONFIG_LIST_ROUTE = "/api/v2/server-configs"
SERVER_CONFIG_DETAIL_ROUTE = "/api/v2/server-configs/{name}"
SERVER_CONFIG_TEST_SSH_ROUTE = "/api/v2/server-configs/test-ssh"
SERVER_CONFIG_ADD_REQUESTS_ROUTE = "/api/v2/server-configs/add-requests"
SERVER_CONFIG_UPDATE_REQUESTS_ROUTE = "/api/v2/server-configs/update-requests"
SERVER_CONFIG_DISABLE_REQUESTS_ROUTE = "/api/v2/server-configs/disable-requests"
SERVER_CONFIG_DELETE_REQUESTS_ROUTE = "/api/v2/server-configs/delete-requests"
INVENTORY_CANDIDATES_ROUTE = "/api/v2/inventory/candidates"
INVENTORY_SCAN_REQUESTS_ROUTE = "/api/v2/inventory/scan-requests"
INVENTORY_CANDIDATE_IMPORT_REQUESTS_ROUTE = (
    "/api/v2/inventory/candidates/{candidate_id}/import-requests"
)
INVENTORY_CANDIDATE_IGNORE_REQUESTS_ROUTE = (
    "/api/v2/inventory/candidates/{candidate_id}/ignore-requests"
)
INVENTORY_CANDIDATES_IGNORE_NESTED_REQUESTS_ROUTE = (
    "/api/v2/inventory/candidates/ignore-nested-requests"
)
CODEX_RUNNER_STATUS_ROUTE = "/api/v2/codex-runner/status"


router = APIRouter(
    prefix=API_V2_PREFIX,
    dependencies=[
        Depends(api_v2_feature_gate),
        Depends(product_rbac_v2_feature_gate),
    ],
)


def _runtime(request: Request) -> Any:
    """The single running `app.main.AppState` instance, duck-typed `Any` --
    matches `jobs_v2._runtime()`."""

    app_state = getattr(request.app.state, "dispatch_runtime", None)
    if app_state is None:
        raise RuntimeError("Dispatch runtime state is unavailable")
    return app_state


def _not_found() -> APIError:
    return APIError(code="not_found", message="Resource not found", status_code=404)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _server_state_to_dict(state: ServerState, app_state: Any) -> dict[str, Any]:
    """Duplicates `app.main._server_state_to_dict` (see module docstring)."""

    d = asdict(state)
    d["gpu_util_max"] = state.gpu_util_max
    d["gpu_count"] = state.gpu_count
    d["cached_datasets"] = [
        f"{c.dataset}@{c.version}"
        for c in app_state.db.list_dataset_cache(server=state.name)
    ]
    cfg = app_state.server_configs.get(state.name)
    d["execution_backend"] = resolve_execution_backend(
        getattr(cfg, "execution_backend", "ssh") if cfg else "ssh",
        node_agent_enabled=(
            app_state.config.node_new_assignment_enabled
            or app_state.config.node_agent_v1_enabled
        ),
    )
    d["enabled"] = bool(getattr(cfg, "enabled", False)) if cfg else False
    return d


def _idle_summary_to_dict(summary: Any) -> dict[str, Any]:
    """Duplicates `app.main._idle_summary_to_dict` (see module docstring)."""

    return {
        "server_name": summary.server_name,
        "window_hours": summary.window_hours,
        "sample_count": summary.sample_count,
        "online_ratio": summary.online_ratio,
        "gpu_util_p50": summary.gpu_util_p50,
        "gpu_util_p95": summary.gpu_util_p95,
        "load1_p50": summary.load1_p50,
        "load1_p95": summary.load1_p95,
        "continuous_idle_seconds": summary.continuous_idle_seconds,
        "freshness_seconds": summary.freshness_seconds,
        "status": summary.status,
    }


def _candidate_to_dict(c: Any) -> dict[str, Any]:
    """Duplicates `app.main._candidate_to_dict` (see module docstring)."""

    return {
        "id": c.id,
        "server": c.server,
        "path": c.path,
        "name_guess": c.name_guess,
        "kind": c.kind,
        "git_remote": c.git_remote,
        "git_branch": c.git_branch,
        "git_commit": c.git_commit,
        "markers": c.markers,
        "readme_excerpt": c.readme_excerpt,
        "command_guess": c.command_guess,
        "embedded_data_paths": c.embedded_data_paths,
        "embedded_data_summary": c.embedded_data_summary,
        "estimated_data_bytes": c.estimated_data_bytes,
        "excluded_paths": c.excluded_paths,
        "confidence": c.confidence,
        "status": c.status,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


def _server_config_with_attempt_evidence(
    cfg: ServerConfig, app_state: Any
) -> dict[str, Any]:
    """Duplicates `app.main._server_config_with_attempt_evidence` (see module
    docstring): safe config projection (key *path*, never contents -- see
    `server_config_to_safe_dict`) plus non-secret revision/preflight
    evidence."""

    projected = server_config_to_safe_dict(cfg)
    revision = app_state.db.get_active_server_config_revision(cfg.name)
    matching_revision = app_state._matching_active_server_revision(
        cfg.name, require_ssh_preflight=False
    )
    projected.update(
        {
            "server_config_revision_id": revision["id"] if revision else None,
            "attempt_backend_preflight": (
                revision["attempt_backend_preflight"] if revision else None
            ),
            "attempt_backend_preflight_observed_at": (
                revision["attempt_backend_preflight_observed_at"]
                if revision
                else None
            ),
            "attempt_backend_preflight_contract_version": (
                revision["attempt_backend_preflight_contract_version"]
                if revision
                else None
            ),
            "attempt_backend_preflight_filesystem_type": (
                revision["attempt_backend_preflight_filesystem_type"]
                if revision
                else None
            ),
            "attempt_backend_eligible": (
                app_state._matching_active_server_revision(cfg.name) is not None
            ),
            "attempt_backend_preflight_available": (
                cfg.execution_backend == "ssh" and matching_revision is not None
            ),
        }
    )
    return projected


# ---------------------------------------------------------------------------
# Servers (read-only health)
# ---------------------------------------------------------------------------


@router.get("/servers")
def list_servers(request: Request, response: Response) -> list[dict[str, Any]]:
    """Wraps legacy `GET /servers`."""

    app_state = _runtime(request)
    _no_store(response)
    return [
        _server_state_to_dict(s, app_state) for s in app_state.server_states.values()
    ]


@router.get("/servers/idle-summary")
async def get_servers_idle_summary(
    request: Request, response: Response, hours: int = 24
) -> dict[str, Any]:
    """Wraps legacy `GET /servers/idle-summary`: deterministic, read-only,
    no SSH, no scheduling effect."""

    app_state = _runtime(request)
    hours = max(1, min(hours, 24 * 30))
    since_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    summaries = []
    for name in sorted(app_state.server_configs):
        server_cfg = app_state.server_configs[name]
        observations = app_state.db.list_server_observations(
            name, since_iso=since_iso, limit=5000
        )
        summary = summarize_observations(
            name,
            observations,
            window_hours=hours,
            now_iso=now_iso,
            gpu_server=server_cfg.gpu,
            idle_gpu_util=server_cfg.idle_gpu_util,
            idle_load=server_cfg.idle_load,
        )
        summaries.append(_idle_summary_to_dict(summary))
    _no_store(response)
    return {"window_hours": hours, "servers": summaries}


# ---------------------------------------------------------------------------
# Server config: read + test-ssh + add/update/disable/delete requests
# ---------------------------------------------------------------------------


@router.get("/server-configs")
def list_server_configs(request: Request, response: Response) -> list[dict[str, Any]]:
    """Wraps legacy `GET /server-config`."""

    app_state = _runtime(request)
    _no_store(response)
    return [
        _server_config_with_attempt_evidence(cfg, app_state)
        for cfg in app_state.server_configs.values()
    ]


@router.get("/server-configs/{name}")
def get_server_config(
    name: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /server-config/{name}`."""

    app_state = _runtime(request)
    cfg = app_state.server_configs.get(name)
    if cfg is None:
        raise _not_found()
    _no_store(response)
    return _server_config_with_attempt_evidence(cfg, app_state)


@router.post("/server-configs/test-ssh")
async def test_server_ssh(
    req: ServerConfigPayload, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /server-config/test-ssh`: a read-only direct probe
    (no approval, no `servers.yaml` write, no `agent_jobs` row) of a payload
    server config, not a stored `name` lookup -- see the legacy docstring for
    the bug this asymmetry originally fixed."""

    app_state = _runtime(request)
    payload = req.model_dump()
    server_cfg = ServerConfig(
        name=payload["name"],
        host=payload["host"],
        user=payload["user"],
        key=payload["key"],
        gpu=payload["gpu"],
        idle_gpu_util=payload["idle_gpu_util"],
        idle_load=payload["idle_load"],
        tags=list(payload["tags"]),
        port=payload["port"],
        project_roots=list(payload["project_roots"]),
        dataset_roots=list(payload["dataset_roots"]),
    )

    _ok, errors, warnings = validate_server_config(payload, app_state.config)
    if errors:
        _no_store(response)
        return {"ok": False, "results": {}, "warnings": warnings, "errors": errors}

    async def _direct_run(_name: str, command: str, timeout: float):
        return await app_state.ssh_pool.run(server_cfg, command, timeout)

    result = await test_ssh_connection(server_cfg, _direct_run)
    result["warnings"] = list(result.get("warnings") or []) + warnings
    append_audit(
        "server_test_ssh",
        {"name": server_cfg.name, "host": server_cfg.host, "ok": result["ok"]},
        result="ok" if result["ok"] else "failed",
        path=app_state.config.audit_path,
        actor=audit_actor_from_request_context(request.state.request_context),
    )
    _no_store(response)
    return result


@router.post("/server-configs/add-requests")
async def request_server_add(
    req: ServerConfigPayload, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /server-config/add-request`: creates a
    `kind=server_add` Approval only, never writes `servers.yaml`."""

    app_state = _runtime(request)
    payload = req.model_dump(exclude_none=True)
    current_document = load_servers_config(app_state.config.servers_yaml_path)
    try:
        approval = approvals_module.request_server_add_approval(
            app_state.db,
            payload,
            app_state.config,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
            current_document=current_document,
        )
    except InvalidServerConfigError as exc:
        raise APIError(
            code="invalid_server_config",
            message="；".join(exc.errors),
            status_code=400,
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/server-configs/update-requests")
async def request_server_update(
    req: ServerUpdateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /server-config/update-request`."""

    app_state = _runtime(request)
    current_document = load_servers_config(app_state.config.servers_yaml_path)
    current_servers = current_document.get("servers") or []
    try:
        approval = approvals_module.request_server_update_approval(
            app_state.db,
            req.name,
            req.updates,
            app_state.config,
            current_servers,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
            current_document=current_document,
        )
    except ServerRenameNotSupportedError as exc:
        raise APIError(
            code="server_rename_not_supported", message=str(exc), status_code=400
        ) from exc
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    except InvalidServerConfigError as exc:
        raise APIError(
            code="invalid_server_config",
            message="；".join(exc.errors),
            status_code=400,
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/server-configs/disable-requests")
async def request_server_disable(
    req: ServerNameRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /server-config/disable-request`."""

    app_state = _runtime(request)
    current_document = load_servers_config(app_state.config.servers_yaml_path)
    current_names = [
        server.get("name")
        for server in current_document.get("servers") or []
        if isinstance(server, dict) and isinstance(server.get("name"), str)
    ]
    try:
        approval = approvals_module.request_server_disable_approval(
            app_state.db,
            req.name,
            current_names,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
            current_document=current_document,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/server-configs/delete-requests")
async def request_server_delete(
    req: ServerNameRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /server-config/delete-request`: approved outcome is
    `enabled=false`, not an actual removal -- see the legacy docstring."""

    app_state = _runtime(request)
    current_document = load_servers_config(app_state.config.servers_yaml_path)
    current_names = [
        server.get("name")
        for server in current_document.get("servers") or []
        if isinstance(server, dict) and isinstance(server.get("name"), str)
    ]
    try:
        approval = approvals_module.request_server_delete_approval(
            app_state.db,
            req.name,
            current_names,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
            current_document=current_document,
        )
    except ServerNotFoundError as exc:
        raise _not_found() from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


# ---------------------------------------------------------------------------
# Inventory: scan / candidates / import / ignore / manual add
# ---------------------------------------------------------------------------


@router.get("/inventory/candidates")
def list_inventory_candidates(
    request: Request,
    response: Response,
    server: Optional[str] = None,
    status: Optional[str] = None,
    q: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Wraps legacy `GET /inventory/candidates`."""

    app_state = _runtime(request)
    _no_store(response)
    return [
        _candidate_to_dict(c)
        for c in app_state.db.list_project_candidates(server=server, status=status, q=q)
    ]


@router.post("/inventory/scan-requests")
async def request_inventory_scan(
    req: InventoryScanRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /inventory/scan`: creates `kind=inventory_scan`
    Approval(s), never scans directly. `server="all"` fans out into one
    Approval per enabled server and returns `{"approvals": [...]}`; a single
    server still returns a single approval dict."""

    app_state = _runtime(request)
    try:
        result = approvals_module.request_inventory_scan_approval(
            app_state.db,
            req.server,
            req.project_roots,
            server_configs=app_state.server_configs,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ForbiddenScanRootError as exc:
        raise APIError(
            code="forbidden_scan_root", message=str(exc), status_code=400
        ) from exc
    except ValueError as exc:
        raise APIError(
            code="inventory_scan_invalid", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    if isinstance(result, list):
        return {
            "approvals": [approvals_module.approval_to_dict(a) for a in result]
        }
    return approvals_module.approval_to_dict(result)


@router.post("/inventory/candidates/{candidate_id}/import-requests")
async def request_import_candidate(
    candidate_id: str,
    req: ImportProjectCandidateRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """Wraps legacy `POST /inventory/candidates/{id}/import-request`: creates
    `kind=import_project` Approval only."""

    app_state = _runtime(request)
    overrides = req.model_dump(exclude_none=True)
    try:
        approval = approvals_module.request_import_project_approval(
            app_state.db,
            candidate_id,
            overrides,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CandidateNotFoundError as exc:
        raise _not_found() from exc
    except CandidateNotPendingError as exc:
        raise APIError(
            code="candidate_not_pending", message=str(exc), status_code=400
        ) from exc
    except ValueError as exc:
        raise APIError(
            code="import_request_invalid", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/inventory/candidates/{candidate_id}/ignore-requests")
async def request_ignore_candidate(
    candidate_id: str, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /inventory/candidates/{id}/ignore-request`: creates
    `kind=ignore_project_candidate` Approval only."""

    app_state = _runtime(request)
    try:
        approval = approvals_module.request_ignore_project_candidate_approval(
            app_state.db,
            candidate_id,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except CandidateNotFoundError as exc:
        raise _not_found() from exc
    except CandidateNotPendingError as exc:
        raise APIError(
            code="candidate_not_pending", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/inventory/candidates/ignore-nested-requests")
async def request_ignore_nested_candidates(
    request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /inventory/candidates/ignore-nested-request`:
    batches every pending candidate nested under another non-ignored
    candidate into a single `kind=ignore_nested_candidates` Approval."""

    app_state = _runtime(request)
    try:
        approval = approvals_module.request_ignore_nested_candidates_approval(
            app_state.db,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except NoNestedCandidatesError as exc:
        raise APIError(
            code="no_nested_candidates", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return approvals_module.approval_to_dict(approval)


@router.post("/inventory/candidates")
async def add_manual_inventory_candidate(
    req: ManualCandidateRequest, request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `POST /inventory/candidates/manual`: **not**
    approval-gated -- directly inserts a `status=pending` `ProjectCandidate`
    row (the real import still goes through `import_project` Approval)."""

    app_state = _runtime(request)
    try:
        candidate = await approvals_module.add_manual_candidate(
            app_state.db,
            req.server,
            req.path,
            req.name,
            server_configs=app_state.server_configs,
            ssh_run=app_state.ssh_run,
            audit_path=app_state.config.audit_path,
            request_context=request.state.request_context,
        )
    except ManualCandidateDuplicateError as exc:
        raise APIError(
            code="manual_candidate_duplicate",
            message=str(exc),
            status_code=409,
            details={"id": exc.existing.id, "status": exc.existing.status},
        ) from exc
    except (ManualCandidateServerInvalidError, ManualCandidatePathInvalidError) as exc:
        raise APIError(
            code="manual_candidate_invalid", message=str(exc), status_code=400
        ) from exc
    except InvalidNameError as exc:
        raise APIError(
            code="manual_candidate_invalid_name", message=str(exc), status_code=400
        ) from exc
    _no_store(response)
    return _candidate_to_dict(candidate)


# ---------------------------------------------------------------------------
# Codex runner status
# ---------------------------------------------------------------------------


@router.get("/codex-runner/status")
async def get_codex_runner_status(
    request: Request, response: Response
) -> dict[str, Any]:
    """Wraps legacy `GET /codex-runner/status`
    (`AppState.get_codex_runner_status()`): never returns `codex login`
    output."""

    app_state = _runtime(request)
    _no_store(response)
    return await app_state.get_codex_runner_status()


__all__ = [
    "CODEX_RUNNER_STATUS_ROUTE",
    "INVENTORY_CANDIDATES_ROUTE",
    "INVENTORY_CANDIDATE_IGNORE_REQUESTS_ROUTE",
    "INVENTORY_CANDIDATE_IMPORT_REQUESTS_ROUTE",
    "INVENTORY_CANDIDATES_IGNORE_NESTED_REQUESTS_ROUTE",
    "INVENTORY_SCAN_REQUESTS_ROUTE",
    "SERVERS_IDLE_SUMMARY_ROUTE",
    "SERVERS_LIST_ROUTE",
    "SERVER_CONFIG_ADD_REQUESTS_ROUTE",
    "SERVER_CONFIG_DELETE_REQUESTS_ROUTE",
    "SERVER_CONFIG_DETAIL_ROUTE",
    "SERVER_CONFIG_DISABLE_REQUESTS_ROUTE",
    "SERVER_CONFIG_LIST_ROUTE",
    "SERVER_CONFIG_TEST_SSH_ROUTE",
    "SERVER_CONFIG_UPDATE_REQUESTS_ROUTE",
    "router",
]
