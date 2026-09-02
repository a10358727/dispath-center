"""Goal 1 Slice 2 interface/action coverage drift tests."""

import ast
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

from app.authorization import Action
from app.authorization_catalog import (
    AGENT_RUNNER_ROUTE_INTERFACES,
    FRAMEWORK_ROUTE_INTERFACES,
    MCP_TOOL_AUTHORIZATION,
    MCP_TOOL_ROUTES,
    NODE_ROUTE_INTERFACES,
    PUBLIC_ROUTE_INTERFACES,
    ROUTE_AUTHORIZATION,
    STUDIO_MOUNT_INTERFACE,
)
from app.main import STATIC_DIR, app
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
    expected_framework = set(FRAMEWORK_ROUTE_INTERFACES)
    if (STATIC_DIR / "studio" / "index.html").is_file():
        expected_framework.add(STUDIO_MOUNT_INTERFACE)
    assert framework_interfaces == expected_framework
    assert registered == (
        set(ROUTE_AUTHORIZATION) | PUBLIC_ROUTE_INTERFACES | NODE_ROUTE_INTERFACES | AGENT_RUNNER_ROUTE_INTERFACES
    )
    #: 三種分類必須互斥——一條路由只能屬於一種。
    assert set(ROUTE_AUTHORIZATION).isdisjoint(PUBLIC_ROUTE_INTERFACES)
    assert set(ROUTE_AUTHORIZATION).isdisjoint(NODE_ROUTE_INTERFACES)
    assert PUBLIC_ROUTE_INTERFACES.isdisjoint(NODE_ROUTE_INTERFACES)
    assert AGENT_RUNNER_ROUTE_INTERFACES.isdisjoint(set(ROUTE_AUTHORIZATION) | PUBLIC_ROUTE_INTERFACES | NODE_ROUTE_INTERFACES)
    assert all(interface[1].startswith("/agent-runner/") for interface in AGENT_RUNNER_ROUTE_INTERFACES)
    # The exact interface count is incidental bookkeeping (it changes on
    # every legitimate route addition). The safety boundary is the set
    # equality above (`registered == ROUTE_AUTHORIZATION | PUBLIC | NODE |
    # AGENT_RUNNER`, checked exactly once above) plus the mutual-exclusivity
    # checks: every registered route is classified into exactly one bucket,
    # so an unauthorized surface cannot appear by accident.


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
    assert len(registered) == len(MCP_TOOL_ACTIONS)


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




