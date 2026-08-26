import ast
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from app.main import app
from dispatch_center.api.routers import ROUTERS
from dispatch_center.api.routers.identity_workspace_v2 import (
    router as identity_workspace_v2_router,
)
from dispatch_center.api.routers.approvals_v2 import router as approvals_v2_router
from dispatch_center.api.routers.project_bootstrap_v2 import (
    router as project_bootstrap_v2_router,
)
from dispatch_center.api.routers.project_environments_v1 import (
    router as project_environments_v1_router,
)
from dispatch_center.api.routers.run_templates_v2 import (
    router as run_templates_v2_router,
)
from dispatch_center.api.routers.dataset_assets_v2 import (
    router as dataset_assets_v2_router,
)
from dispatch_center.api.routers.project_roles_v2 import (
    router as project_roles_v2_router,
)
from dispatch_center.api.routers.project_instance_update_v2 import (
    router as project_instance_update_v2_router,
)
from dispatch_center.api.routers.runs_v2 import router as runs_v2_router
from dispatch_center.api.routers.experiments_v2 import (
    router as experiments_v2_router,
)
from dispatch_center.api.routers.jobs_v2 import router as jobs_v2_router
from dispatch_center.api.routers.infrastructure_v2 import (
    router as infrastructure_v2_router,
)
from dispatch_center.api.routers.projects_legacy_v2 import (
    router as projects_legacy_v2_router,
)
from dispatch_center.api.routers.engineering_v2 import (
    router as engineering_v2_router,
)
from dispatch_center.api.routers.ai_providers_v2 import (
    router as ai_providers_v2_router,
)
from dispatch_center.api.routers.v2 import router as v2_router


def test_http_and_websocket_routes_are_owned_by_bounded_routers():
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    direct_route_decorators = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            function = decorator.func
            if not isinstance(function, ast.Attribute):
                continue
            if not isinstance(function.value, ast.Name) or function.value.id != "app":
                continue
            if function.attr in {
                "delete",
                "get",
                "head",
                "options",
                "patch",
                "post",
                "put",
                "trace",
                "websocket",
            }:
                direct_route_decorators.append((decorator.lineno, function.attr))

    assert direct_route_decorators == []
    assert len(ROUTERS) == 12
    assert v2_router.routes == []
    assert v2_router.prefix == "/api/v2"
    assert all(router.routes for router in ROUTERS)
    # 144 = 134 pre-extraction routes + PERSONAL_PILOT_PLAN §6 T2's two
    # read-only job-results routes (list + single-file download) on runs_router
    # + DG-CONVERSATION-V1 CV-2a's two per-project AI conversation routes
    # (GET conversation, POST conversation/messages) on projects_router
    # + DG-AGENT-SESSION-V1 P1's three AgentSession routes (GET/POST on
    # projects_router, POST close on engineering_router)
    # + DG-AGENT-SESSION-V1 P2's two per-turn routes (POST messages, GET
    # transcript) on engineering_router
    # + DG-AGENT-SESSION-V1 P3's one read-only session diff route (GET diff)
    # on engineering_router
    # + DG-AGENT-SESSION-CHECKPOINT's one checkpoint-request route (POST
    # checkpoint-request) on engineering_router
    # + DG-METRICS-CONTRACT v1's one read-only job metrics route (GET
    # jobs/{job_id}/metrics) on runs_router
    # + DG-UI-UNIFICATION v1 U8's two thin `/api/v2/events`/`/api/v2/audit`
    # wrapper routes on operations_router (defined in `app/main.py` next to
    # the legacy `/events`/`/audit` handlers they wrap, for the same
    # closure-over-`app_state` reason those two are also defined there
    # instead of in a separate router module).
    assert sum(isinstance(route, APIRoute) for router in ROUTERS for route in router.routes) == 148
    assert sum(isinstance(route, WebSocketRoute) for router in ROUTERS for route in router.routes) == 1
    included_routers = [
        route.original_router
        for route in app.routes
        if hasattr(route, "original_router")
    ]
    assert included_routers == [
        *ROUTERS,
        v2_router,
        identity_workspace_v2_router,
        approvals_v2_router,
        project_bootstrap_v2_router,
        project_environments_v1_router,
        run_templates_v2_router,
        dataset_assets_v2_router,
        runs_v2_router,
        project_instance_update_v2_router,
        experiments_v2_router,
        project_roles_v2_router,
        jobs_v2_router,
        infrastructure_v2_router,
        projects_legacy_v2_router,
        engineering_v2_router,
        ai_providers_v2_router,
    ]
