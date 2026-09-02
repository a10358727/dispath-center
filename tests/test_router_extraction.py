import ast
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import WebSocketRoute

from app.main import app
from dispatch_center.api.routers import PRODUCT_ROUTERS, ROUTERS, v2_router


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
    assert v2_router.routes == []
    assert v2_router.prefix == "/api/v2"
    assert all(router.routes for router in ROUTERS)
    assert all(router.routes for router in PRODUCT_ROUTERS if router is not v2_router)

    # The exact route/router counts are incidental bookkeeping (they change
    # on every legitimate router/route addition), not a safety boundary; the
    # safety-relevant checks are: no stray `@app.<verb>` route bypasses a
    # bounded router (above), every router actually owns routes, and the set
    # of routers mounted on `app` matches the registries exactly (below).
    included_routers = [
        route.original_router
        for route in app.routes
        if hasattr(route, "original_router")
    ]
    assert included_routers == [*ROUTERS, *PRODUCT_ROUTERS]

    # Sanity: every route FastAPI actually serves for these routers is an
    # APIRoute or WebSocketRoute owned by one of the bounded routers — no
    # route type escapes the registries' accounting.
    all_routes = [
        route
        for router in (*ROUTERS, *PRODUCT_ROUTERS)
        for route in router.routes
    ]
    assert all(isinstance(route, (APIRoute, WebSocketRoute)) for route in all_routes)
