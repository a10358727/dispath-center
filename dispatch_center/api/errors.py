"""Stable error-envelope primitives for extracted API routes.

Legacy routes retain their historical ``{"detail": ...}`` responses during
router extraction. New or deliberately migrated use cases raise ``APIError``
so callers never receive a raw exception or depend on display text as a code.
"""

from __future__ import annotations

from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dispatch_center.api.request_id import request_id_from_scope


class APIError(Exception):
    """A reviewed, client-safe API failure."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = dict(details or {})


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Serialize only the explicit public fields carried by ``APIError``."""

    if not isinstance(exc, APIError):  # pragma: no cover - registration guard
        raise exc
    request_id = request_id_from_scope(request.scope)
    return JSONResponse(
        status_code=exc.status_code,
        headers={"X-Request-ID": request_id},
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "request_id": request_id,
                "details": exc.details,
            }
        },
    )


def install_api_error_handlers(app: FastAPI) -> None:
    """Install the additive error contract without rewriting legacy errors."""

    app.add_exception_handler(APIError, api_error_handler)
