from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request

from app.config import AppConfig
from app.main import _feature_gate_disabled_for_route, app
from dispatch_center.api.errors import APIError, install_api_error_handlers
from scripts.openapi_snapshot import build_snapshot, diff_lines, load_snapshot
from dispatch_center.api.pagination import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    build_cursor_page,
    decode_cursor,
    encode_cursor,
    pagination_query_sha256,
    validate_page_limit,
)
from dispatch_center.api.request_id import RequestIdMiddleware
from dispatch_center.api.routers import ROUTERS
from dispatch_center.api.routers.v2 import create_v2_router, router as v2_router


def _synthetic_v2_app(*, enabled: bool) -> FastAPI:
    synthetic = FastAPI()
    synthetic.state.dispatch_config = SimpleNamespace(api_v2_enabled=enabled)
    install_api_error_handlers(synthetic)
    router = create_v2_router()

    @router.get("/synthetic")
    async def synthetic_route():
        return {"ok": True}

    synthetic.include_router(router)
    synthetic.add_middleware(RequestIdMiddleware)
    return synthetic


@pytest.mark.asyncio
async def test_v2_gate_hides_matched_routes_with_stable_error_and_registration():
    disabled = _synthetic_v2_app(enabled=False)
    enabled = _synthetic_v2_app(enabled=True)
    assert set(disabled.openapi()["paths"]) == {"/api/v2/synthetic"}
    assert disabled.openapi()["paths"] == enabled.openapi()["paths"]

    disabled_transport = httpx.ASGITransport(app=disabled)
    enabled_transport = httpx.ASGITransport(app=enabled)
    async with (
        httpx.AsyncClient(transport=disabled_transport, base_url="http://test") as disabled_client,
        httpx.AsyncClient(transport=enabled_transport, base_url="http://test") as enabled_client,
    ):
        hidden = await disabled_client.get("/api/v2/synthetic")
        visible = await enabled_client.get("/api/v2/synthetic")
        disabled_root = await disabled_client.get("/api/v2")
        enabled_root = await enabled_client.get("/api/v2/")

    assert hidden.status_code == 404
    assert hidden.json() == {
        "error": {
            "code": "not_found",
            "message": "Resource not found",
            "request_id": hidden.headers["X-Request-ID"],
            "details": {},
        }
    }
    assert visible.status_code == 200
    assert visible.json() == {"ok": True}
    assert disabled_root.status_code == 404
    assert enabled_root.status_code == 404


def test_v2_root_stays_empty_while_product_routes_are_additive():
    schema = app.openapi()

    assert isinstance(v2_router, APIRouter)
    assert v2_router.prefix == "/api/v2"
    assert v2_router.routes == []
    assert v2_router not in ROUTERS
    assert {
        "/api/v2/me",
        "/api/v2/me/sessions",
        "/api/v2/workspace",
        "/api/v2/projects/{project_id}/roles",
        "/api/v2/projects/{project_id}/role-change-requests",
        "/api/v2/projects/{project_id}/run-templates",
        "/api/v2/projects/{project_id}/run-template-change-requests",
        "/api/v2/projects/{project_id}/defaults",
        "/api/v2/projects/{project_id}/default-change-requests",
        "/api/v2/projects/{project_id}/instance-update-previews",
        "/api/v2/projects/{project_id}/instance-update-requests",
        "/api/v2/approvals/{approval_id}/decisions",
    } <= set(schema["paths"])
    # 整頓 C2b: the surface is pinned by the readable JSON snapshot (tests/openapi_snapshot.json).
    assert diff_lines(load_snapshot(), build_snapshot()) == []


def test_authorization_dependency_defers_to_disabled_v2_route_gate():
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v2/projects/project-1",
            "headers": [],
            "route": SimpleNamespace(path="/api/v2/projects/{project_id}"),
        }
    )

    assert _feature_gate_disabled_for_route(request, AppConfig(servers=[], api_v2_enabled=False))
    assert not _feature_gate_disabled_for_route(request, AppConfig(servers=[], api_v2_enabled=True))


def test_cursor_round_trip_binds_actor_route_sort_and_filters():
    query_digest = pagination_query_sha256(
        actor_id="actor-1",
        route_template="/api/v2/projects/{project_id}/datasets",
        sort_contract="created_at-asc,id-asc-v1",
        filters={"project_id": "project-1", "state": "published"},
    )
    encoded = encode_cursor(
        ("2026-08-07T01:02:03.000000Z", 42),
        query_sha256=query_digest,
    )

    assert "=" not in encoded
    assert decode_cursor(
        encoded,
        expected_query_sha256=query_digest,
        key_types=(str, int),
    ) == ("2026-08-07T01:02:03.000000Z", 42)
    assert query_digest != pagination_query_sha256(
        actor_id="actor-2",
        route_template="/api/v2/projects/{project_id}/datasets",
        sort_contract="created_at-asc,id-asc-v1",
        filters={"project_id": "project-1", "state": "published"},
    )


@pytest.mark.parametrize("limit", [1, DEFAULT_PAGE_LIMIT, MAX_PAGE_LIMIT])
def test_page_limit_accepts_the_bounded_contract(limit):
    assert validate_page_limit(limit) == limit


@pytest.mark.parametrize("limit", [True, 0, 101, -1, 1.5, "50"])
def test_page_limit_rejects_invalid_values_with_422(limit):
    with pytest.raises(APIError) as raised:
        validate_page_limit(limit)
    assert raised.value.status_code == 422
    assert raised.value.code == "invalid_page_limit"


def test_cursor_page_uses_unique_tie_breaker_and_limit_plus_one():
    rows = [
        {"created_at": "2026-08-07T00:00:00Z", "id": 1},
        {"created_at": "2026-08-07T00:00:00Z", "id": 2},
        {"created_at": "2026-08-07T00:00:01Z", "id": 3},
    ]
    query_digest = "a" * 64
    page = build_cursor_page(
        rows,
        limit=2,
        query_sha256=query_digest,
        cursor_keys=lambda row: (row["created_at"], row["id"]),
    )

    assert page.items == tuple(rows[:2])
    assert page.next_cursor is not None
    position = decode_cursor(
        page.next_cursor,
        expected_query_sha256=query_digest,
        key_types=(str, int),
    )
    assert position == ("2026-08-07T00:00:00Z", 2)
    assert [row for row in rows if (row["created_at"], row["id"]) > position] == [rows[2]]


def test_cursor_page_rejects_an_unbounded_query_result():
    with pytest.raises(ValueError, match=r"at most limit \+ 1"):
        build_cursor_page(
            (1, 2, 3, 4),
            limit=2,
            query_sha256="a" * 64,
            cursor_keys=lambda value: (value,),
        )


def _encoded_payload(payload: object, *, canonical: bool = True) -> str:
    if canonical:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    else:
        raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.mark.parametrize(
    "encoded,key_types",
    [
        ("not+base64url", None),
        (_encoded_payload({"k": [1], "q": "a" * 64, "v": 2}), None),
        (_encoded_payload({"k": [], "q": "a" * 64, "v": 1}), None),
        (_encoded_payload({"k": [True], "q": "a" * 64, "v": 1}), None),
        (_encoded_payload({"k": [2**63], "q": "a" * 64, "v": 1}), None),
        (_encoded_payload({"k": [1], "q": "a" * 64, "v": 1}, canonical=False), None),
        (_encoded_payload({"extra": 1, "k": [1], "q": "a" * 64, "v": 1}), None),
        (_encoded_payload({"k": [1], "q": "a" * 64, "v": 1}), (str,)),
    ],
)
def test_cursor_rejects_malformed_noncanonical_or_type_mismatched_values(encoded, key_types):
    with pytest.raises(APIError) as raised:
        decode_cursor(
            encoded,
            expected_query_sha256="a" * 64,
            key_types=key_types,
        )
    assert raised.value.status_code == 400
    assert raised.value.code == "invalid_cursor"


def test_cursor_query_mismatch_never_falls_back_to_the_first_page():
    encoded = encode_cursor((1,), query_sha256="a" * 64)
    with pytest.raises(APIError) as raised:
        decode_cursor(encoded, expected_query_sha256="b" * 64)
    assert raised.value.code == "invalid_cursor"
