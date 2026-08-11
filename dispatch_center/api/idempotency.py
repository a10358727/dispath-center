"""Validated Product API v2 idempotency request identities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from fastapi import Request

from app.execution_contract import canonical_json_sha256
from app.identity import RequestContext
from dispatch_center.api.errors import APIError


IDEMPOTENCY_HEADER = "Idempotency-Key"
IDEMPOTENCY_TTL_SECONDS = 86_400
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _validate_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be lowercase SHA-256 hex")


@dataclass(frozen=True)
class IdempotencyIdentity:
    actor_id: str
    route_key: str
    key_sha256: str
    request_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id:
            raise ValueError("actor_id must be non-empty text")
        if not isinstance(self.route_key, str) or not 1 <= len(self.route_key) <= 512:
            raise ValueError("route_key must contain between 1 and 512 characters")
        _validate_sha256(self.key_sha256, "key_sha256")
        _validate_sha256(self.request_sha256, "request_sha256")


@dataclass(frozen=True)
class IdempotencyResource:
    resource_type: str
    resource_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.resource_type, str) or not 1 <= len(self.resource_type) <= 128:
            raise ValueError("idempotency resource_type must contain between 1 and 128 characters")
        if not isinstance(self.resource_id, str) or not 1 <= len(self.resource_id) <= 512:
            raise ValueError("idempotency resource_id must contain between 1 and 512 characters")


@dataclass(frozen=True)
class IdempotencyOutcome:
    resource: IdempotencyResource
    replayed: bool


@dataclass(frozen=True)
class IdempotencyRequestContext:
    """Header, actor, and route facts captured before payload binding."""

    actor_id: str
    method: str
    route_template: str
    key_sha256: str

    def bind(
        self,
        *,
        body: Any,
        path: Mapping[str, Any],
        query: Mapping[str, Any],
    ) -> IdempotencyIdentity:
        request_sha256 = canonical_request_sha256(
            body=body,
            method=self.method,
            path=path,
            query=query,
            route_template=self.route_template,
        )
        return IdempotencyIdentity(
            actor_id=self.actor_id,
            route_key=f"{self.method} {self.route_template}",
            key_sha256=self.key_sha256,
            request_sha256=request_sha256,
        )


def validate_idempotency_key_values(values: Sequence[str]) -> str:
    """Require exactly one visible-ASCII header value without normalization."""

    if not values:
        raise APIError(
            code="idempotency_key_required",
            message="Idempotency-Key is required",
            status_code=400,
        )
    if len(values) != 1:
        raise APIError(
            code="invalid_idempotency_key",
            message="Idempotency-Key must appear exactly once",
            status_code=400,
        )
    value = values[0]
    try:
        raw = value.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        raw = b""
    if not 1 <= len(raw) <= 255 or any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise APIError(
            code="invalid_idempotency_key",
            message="Idempotency-Key must be 1-255 visible ASCII bytes",
            status_code=400,
        )
    return value


def idempotency_key_sha256(value: str) -> str:
    validated = validate_idempotency_key_values((value,))
    return hashlib.sha256(validated.encode("ascii")).hexdigest()


def canonical_request_sha256(
    *,
    body: Any,
    method: str,
    path: Mapping[str, Any],
    query: Mapping[str, Any],
    route_template: str,
) -> str:
    """Hash only validated, normalized mutation inputs."""

    normalized_method = method.upper() if isinstance(method, str) else ""
    if normalized_method not in _MUTATION_METHODS:
        raise ValueError("idempotency request method must be a material mutation")
    if (
        not isinstance(route_template, str)
        or not route_template.startswith("/api/v2/")
        or len(f"{normalized_method} {route_template}") > 512
    ):
        raise ValueError("idempotency route must be a stable /api/v2 template")
    if not isinstance(path, Mapping) or not isinstance(query, Mapping):
        raise ValueError("idempotency path and query values must be mappings")
    return canonical_json_sha256(
        {
            "body": body,
            "method": normalized_method,
            "path": dict(path),
            "query": dict(query),
            "route": route_template,
            "v": "api-idempotency-request-v1",
        }
    )


async def idempotency_context_dependency(
    request: Request,
) -> IdempotencyRequestContext:
    """FastAPI dependency for future v2 mutation routes.

    Payload hashing intentionally remains in ``bind`` so handlers pass their
    schema-validated body, path, and mutation-affecting query values.
    """

    context = getattr(request.state, "request_context", None)
    actor_id = context.actor_id if isinstance(context, RequestContext) else None
    if not isinstance(actor_id, str) or not actor_id:
        raise APIError(
            code="authentication_required",
            message="Authentication is required",
            status_code=401,
        )
    method = request.method.upper()
    if method not in _MUTATION_METHODS:
        raise ValueError("idempotency dependency is only valid for mutation routes")
    route = request.scope.get("route")
    route_template = getattr(route, "path", None)
    if not isinstance(route_template, str) or not route_template.startswith("/api/v2/"):
        raise RuntimeError("idempotency dependency requires a stable v2 route template")
    values = request.headers.getlist(IDEMPOTENCY_HEADER)
    key = validate_idempotency_key_values(values)
    return IdempotencyRequestContext(
        actor_id=actor_id,
        method=method,
        route_template=route_template,
        key_sha256=idempotency_key_sha256(key),
    )


__all__ = [
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENCY_TTL_SECONDS",
    "IdempotencyIdentity",
    "IdempotencyOutcome",
    "IdempotencyRequestContext",
    "IdempotencyResource",
    "canonical_request_sha256",
    "idempotency_context_dependency",
    "idempotency_key_sha256",
    "validate_idempotency_key_values",
]
