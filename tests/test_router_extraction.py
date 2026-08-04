import ast
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from app.main import app
from dispatch_center.api.routers import ROUTERS


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
    assert all(router.routes for router in ROUTERS)
    assert sum(isinstance(route, APIRoute) for router in ROUTERS for route in router.routes) == 133
    assert sum(isinstance(route, WebSocketRoute) for router in ROUTERS for route in router.routes) == 1
    included_routers = [
        route.original_router
        for route in app.routes
        if hasattr(route, "original_router")
    ]
    assert included_routers == list(ROUTERS)
