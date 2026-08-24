"""Goal 1 Slice 2 interface/action coverage drift tests."""

import ast
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

from app.authorization import Action
from app.authorization_catalog import (
    FRAMEWORK_ROUTE_INTERFACES,
    LOCAL_TOOL_AUTHORIZATION,
    LOCAL_TOOL_RESOURCES,
    MCP_TOOL_AUTHORIZATION,
    MCP_TOOL_ROUTES,
    NODE_ROUTE_INTERFACES,
    PUBLIC_ROUTE_INTERFACES,
    ROUTE_AUTHORIZATION,
)
from app.agent_tools import TOOLS
from app.authorization_shadow import SUPPORTED_RESOURCE_KINDS
from app.main import app
from app.mcp_bridge import BridgeConfig, MCP_TOOL_ACTIONS, _build_mcp


def _iter_registered_routes(routes):
    """Flatten FastAPI's router-inclusion wrappers for interface inspection."""

    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            yield from _iter_registered_routes(route.original_router.routes)
        else:
            yield route


def _registered_application_interfaces():
    interfaces = set()
    framework_interfaces = set()
    for route in _iter_registered_routes(app.routes):
        if isinstance(route, APIRoute):
            interfaces.update((method, route.path) for method in route.methods)
        elif isinstance(route, WebSocketRoute):
            interfaces.add(("WEBSOCKET", route.path))
        elif isinstance(route, Mount):
            framework_interfaces.add(
                (type(route).__name__, route.path, route.name, ())
            )
        elif type(route).__name__ == "Route":
            framework_interfaces.add(
                (
                    type(route).__name__,
                    route.path,
                    route.name,
                    tuple(sorted(route.methods or ())),
                )
            )
        else:  # pragma: no cover - fail with a useful value if FastAPI adds one
            raise AssertionError(f"unclassified route object: {route!r}")
    return interfaces, framework_interfaces


def test_every_application_route_has_exactly_one_action_or_public_classification():
    registered, framework_interfaces = _registered_application_interfaces()
    assert framework_interfaces == FRAMEWORK_ROUTE_INTERFACES
    assert registered == (
        set(ROUTE_AUTHORIZATION) | PUBLIC_ROUTE_INTERFACES | NODE_ROUTE_INTERFACES
    )
    #: 三種分類必須互斥——一條路由只能屬於一種。
    assert set(ROUTE_AUTHORIZATION).isdisjoint(PUBLIC_ROUTE_INTERFACES)
    assert set(ROUTE_AUTHORIZATION).isdisjoint(NODE_ROUTE_INTERFACES)
    assert PUBLIC_ROUTE_INTERFACES.isdisjoint(NODE_ROUTE_INTERFACES)
    # 105 HTTP interfaces (Goal 3 Phase B adds 2, A1 adds 1, WP-2A adds one
    # read-only execution-control status) plus WS /ws, plus Goal 3 C2/C3
    # 5 operator + 7 agent interfaces, plus RB-SERVER-001's 2 operator
    # surfaces (journal read + recovery_hold resolution), plus WP-3B's 3 plan
    # surfaces (preview, run request, run view), plus Phase 6's 2 health
    # surfaces (liveness, readiness), plus DG-NODE-V2's current-attempt
    # recovery route on the node channel, plus WP-3A's snapshot request/list/
    # detail/resume surfaces, plus Phase 6's read-only operational metrics
    # surface, plus D-5's revision-scoped filesystem preflight, plus Product v2
    # PR-02's roles read, role-change request, and approval-decision surfaces,
    # plus PR-03's self, session read-view, and My Workspace surfaces, plus
    # PR-04's bootstrap preview/request, Project Workspace, and two Product
    # approval review surfaces, plus PR-05's Environment head read and
    # approval-backed change request surfaces, plus PR-06's Run Template and
    # Project Defaults read/request surfaces, plus PR-07's seven Dataset asset,
    # adoption, alias, lineage, usage, and storage surfaces, plus PR-08's three
    # sharing request surfaces, plus PR-09's read-only publish preview and
    # approval request surfaces, plus PR-10's ExecutionPlan v2 preview and
    # submit surfaces, plus PR-11's Product Run detail, Clone preview, Compare,
    # Stop request, and Artifact metadata surfaces, plus PR-12's existing-
    # instance update preview and approval-request surfaces, plus
    # PERSONAL_PILOT_PLAN.md §6 T2's job results list and single-file download
    # surfaces, plus DG-CONVERSATION-V1 CV-2a's per-project AI conversation
    # read and message-turn surfaces, plus DG-AGENT-SESSION-V1 P1's
    # AgentSession list, open-request, and close surfaces, plus P2's
    # per-turn message and transcript surfaces.
    #
    # This count is a deliberate gate: a new route must be classified in the
    # authorization catalog and consciously counted here, so an unauthorized
    # surface cannot appear by accident.
    assert len(registered) == 182


def test_node_channel_is_never_public_and_never_actor_authorized():
    """Goal 3 C2（INV-NODE-1）：node 通道既不是公開路由，也不掛任何 actor
    action——它由獨立的 node 憑證驗證，撤銷只影響單一 node。"""
    assert NODE_ROUTE_INTERFACES.isdisjoint(PUBLIC_ROUTE_INTERFACES)
    for interface in NODE_ROUTE_INTERFACES:
        assert interface not in ROUTE_AUTHORIZATION
        assert interface[1].startswith("/node-agent/")


def test_oidc_handshake_is_the_only_new_public_route_scope():
    assert PUBLIC_ROUTE_INTERFACES == {
        ("GET", "/"),
        ("GET", "/auth/login"),
        ("GET", "/auth/callback"),
    }
    assert ROUTE_AUTHORIZATION[("POST", "/auth/logout")].action is Action.IDENTITY_SELF_VIEW
    assert ROUTE_AUTHORIZATION[("POST", "/auth/logout")].resource_kind == "identity_self"


def test_runtime_policy_evaluator_calls_are_confined_to_fail_open_shadow_module():
    app_dir = Path(__file__).parents[1] / "app"
    callers = []
    for path in app_dir.glob("*.py"):
        if path.name == "authorization.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            if (
                isinstance(called, ast.Name)
                and called.id == "evaluate_authorization"
            ) or (
                isinstance(called, ast.Attribute)
                and called.attr == "evaluate_authorization"
            ):
                callers.append(f"{path.name}:{node.lineno}")
    assert len(callers) == 1
    assert callers[0].startswith("authorization_shadow.py:")


def test_route_catalog_uses_only_declared_actions_and_nonempty_resource_kinds():
    for spec in ROUTE_AUTHORIZATION.values():
        assert isinstance(spec.action, Action)
        assert spec.resource_kind


def test_identity_admin_and_membership_routes_have_exact_goal_1_metadata():
    expected = {
        ("GET", "/identity/service-accounts"): (Action.IDENTITY_MANAGE, "platform"),
        ("POST", "/identity/service-accounts/request"): (
            Action.IDENTITY_MANAGE,
            "platform",
        ),
        ("POST", "/identity/service-accounts/{actor_id}/tokens/request"): (
            Action.IDENTITY_MANAGE,
            "platform",
        ),
        ("POST", "/identity/service-tokens/{token_id}/revoke-request"): (
            Action.IDENTITY_MANAGE,
            "platform",
        ),
        ("GET", "/projects/{name}/memberships"): (
            Action.PROJECT_MEMBERSHIP_MANAGE,
            "project",
        ),
        ("POST", "/projects/{name}/memberships/request"): (
            Action.PROJECT_MEMBERSHIP_MANAGE,
            "project",
        ),
        ("POST", "/projects/{name}/memberships/{actor_id}/remove-request"): (
            Action.PROJECT_MEMBERSHIP_MANAGE,
            "project",
        ),
    }

    assert {
        interface: (
            ROUTE_AUTHORIZATION[interface].action,
            ROUTE_AUTHORIZATION[interface].resource_kind,
        )
        for interface in expected
    } == expected


def test_engineering_task_routes_have_exact_slice3_metadata():
    expected = {
        ("POST", "/projects/{name}/engineering-tasks/request"): (
            Action.PROJECT_OPERATE,
            "project",
        ),
        ("GET", "/engineering-tasks/capabilities"): (
            Action.PLATFORM_VIEW,
            "platform",
        ),
        ("GET", "/coding-agents"): (
            Action.PLATFORM_VIEW,
            "platform",
        ),
        ("GET", "/engineering-tasks"): (
            Action.PROJECT_VIEW,
            "engineering_task_collection",
        ),
        ("GET", "/engineering-tasks/{task_id}"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("POST", "/engineering-tasks/{task_id}/promote-request"): (
            Action.PROJECT_OPERATE,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/attempts"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/events"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/commands"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/commands/{command_id}/log"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/artifacts"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/artifacts/{artifact_id}"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/diff"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
        ("GET", "/engineering-tasks/{task_id}/patch"): (
            Action.PROJECT_VIEW,
            "engineering_task",
        ),
    }

    assert {
        interface: (
            ROUTE_AUTHORIZATION[interface].action,
            ROUTE_AUTHORIZATION[interface].resource_kind,
        )
        for interface in expected
    } == expected
    assert {"engineering_task", "engineering_task_collection"} <= set(
        SUPPORTED_RESOURCE_KINDS
    )


def test_every_catalog_resource_kind_has_an_exact_shadow_resolver():
    catalog_kinds = {
        spec.resource_kind for spec in ROUTE_AUTHORIZATION.values()
    } | set(LOCAL_TOOL_RESOURCES.values())
    assert catalog_kinds == set(SUPPORTED_RESOURCE_KINDS)


def test_every_local_tool_has_action_metadata_without_changing_public_tool_shape():
    assert set(TOOLS) == set(LOCAL_TOOL_AUTHORIZATION)
    assert set(TOOLS) == set(LOCAL_TOOL_RESOURCES)
    for name, spec in TOOLS.items():
        assert spec.authorization_action == LOCAL_TOOL_AUTHORIZATION[name].value
        assert spec.authorization_action in {action.value for action in Action}
        assert spec.authorization_resource == LOCAL_TOOL_RESOURCES[name]
        assert spec.authorization_resource


def test_every_mcp_tool_has_isolated_string_action_metadata():
    config = BridgeConfig(
        dispatch_base_url="http://dispatch.invalid",
        auth_token=None,
        port=1,
        path_secret="test-secret",
        bridge_token=None,
    )
    mcp = _build_mcp(config)
    registered = set(mcp._tool_manager._tools)

    assert registered == set(MCP_TOOL_ACTIONS) == set(MCP_TOOL_AUTHORIZATION)
    assert MCP_TOOL_ACTIONS == {
        name: action.value for name, action in MCP_TOOL_AUTHORIZATION.items()
    }
    assert len(registered) == 25


def test_every_mcp_tool_maps_to_an_underlying_route_with_the_same_action():
    assert set(MCP_TOOL_ROUTES) == set(MCP_TOOL_AUTHORIZATION)
    for tool_name, route_interface in MCP_TOOL_ROUTES.items():
        assert route_interface in ROUTE_AUTHORIZATION
        assert (
            ROUTE_AUTHORIZATION[route_interface].action
            is MCP_TOOL_AUTHORIZATION[tool_name]
        )


def test_every_protected_http_route_has_the_global_shadow_dependency():
    from app.main import _authorization_shadow_dependency

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        interfaces = {(method, route.path) for method in route.methods}
        if not interfaces & set(ROUTE_AUTHORIZATION):
            continue
        dependency_calls = {dependency.call for dependency in route.dependant.dependencies}
        assert _authorization_shadow_dependency in dependency_calls, route.path


def _calls_in_function(path: Path, function_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    calls = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            calls.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return calls


def test_ws_and_local_dispatch_have_their_single_post_auth_shadow_seams():
    app_dir = Path(__file__).parents[1] / "app"
    ws_calls = _calls_in_function(app_dir / "main.py", "ws_endpoint")
    dispatch_calls = _calls_in_function(
        app_dir / "agent_tools.py", "dispatch_tool"
    )

    assert {"collect_shadow_evidence", "emit_shadow_evidence"} <= ws_calls
    assert {"collect_shadow_evidence", "emit_shadow_evidence"} <= dispatch_calls
