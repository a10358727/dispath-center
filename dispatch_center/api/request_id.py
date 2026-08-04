"""Server-generated request correlation for every HTTP response."""

from __future__ import annotations

import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_STATE_KEY = "request_id"


def new_request_id() -> str:
    return str(uuid.uuid4())


def request_id_from_scope(scope: Scope) -> str:
    state = scope.setdefault("state", {})
    request_id = state.get(REQUEST_ID_STATE_KEY)
    if not isinstance(request_id, str) or not request_id:
        request_id = new_request_id()
        state[REQUEST_ID_STATE_KEY] = request_id
    return request_id


class RequestIdMiddleware:
    """Attach an untrusted-input-independent correlation ID to HTTP traffic."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = request_id_from_scope(scope)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        await self.app(scope, receive, send_with_request_id)
