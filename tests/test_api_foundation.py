import re

import httpx
import pytest
from fastapi import FastAPI, Request

from dispatch_center.api.errors import APIError, install_api_error_handlers
from dispatch_center.api.request_id import RequestIdMiddleware


UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _test_app() -> FastAPI:
    app = FastAPI()
    install_api_error_handlers(app)

    @app.get("/request-id")
    async def request_id_endpoint(request: Request):
        return {"request_id": request.state.request_id}

    @app.get("/failure")
    async def failure_endpoint():
        raise APIError(
            code="plan_digest_mismatch",
            message="Execution plan verification failed",
            status_code=409,
            details={"retryable": False},
        )

    app.add_middleware(RequestIdMiddleware)
    return app


@pytest.mark.asyncio
async def test_request_ids_are_server_generated_unique_and_returned_to_callers():
    transport = httpx.ASGITransport(app=_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/request-id", headers={"X-Request-ID": "spoofed"})
        second = await client.get("/request-id")

    first_id = first.headers["X-Request-ID"]
    second_id = second.headers["X-Request-ID"]
    assert UUID_PATTERN.fullmatch(first_id)
    assert UUID_PATTERN.fullmatch(second_id)
    assert first_id != "spoofed"
    assert first_id != second_id
    assert first.json() == {"request_id": first_id}
    assert second.json() == {"request_id": second_id}


@pytest.mark.asyncio
async def test_api_error_has_stable_code_message_details_and_request_id():
    transport = httpx.ASGITransport(app=_test_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/failure")

    request_id = response.headers["X-Request-ID"]
    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "plan_digest_mismatch",
            "message": "Execution plan verification failed",
            "request_id": request_id,
            "details": {"retryable": False},
        }
    }
