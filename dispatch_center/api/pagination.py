"""Versioned, bounded cursor primitives for Product API v2 list routes."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Generic, Mapping, Sequence, TypeVar

from app.execution_contract import canonical_json, canonical_json_sha256
from dispatch_center.api.errors import APIError


DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_CURSOR_BYTES = 2048
_CURSOR_VERSION = 1
_CURSOR_FIELDS = frozenset({"k", "q", "v"})
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1

CursorScalar = int | str
ItemT = TypeVar("ItemT")


def _invalid_cursor() -> APIError:
    return APIError(
        code="invalid_cursor",
        message="The pagination cursor is invalid",
        status_code=400,
    )


def validate_page_limit(limit: int = DEFAULT_PAGE_LIMIT) -> int:
    """Validate the shared v2 page-size contract."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_LIMIT:
        raise APIError(
            code="invalid_page_limit",
            message="Page limit must be between 1 and 100",
            status_code=422,
            details={"default": DEFAULT_PAGE_LIMIT, "maximum": MAX_PAGE_LIMIT},
        )
    return limit


def pagination_query_sha256(
    *,
    actor_id: str,
    route_template: str,
    sort_contract: str,
    filters: Mapping[str, Any],
) -> str:
    """Bind a cursor to caller scope, route, ordering, and normalized filters."""

    for value, field_name in (
        (actor_id, "actor_id"),
        (route_template, "route_template"),
        (sort_contract, "sort_contract"),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} must be non-empty text")
    if not isinstance(filters, Mapping):
        raise ValueError("filters must be a mapping")
    return canonical_json_sha256(
        {
            "actor_id": actor_id,
            "filters": dict(filters),
            "route": route_template,
            "sort": sort_contract,
            "v": "api-cursor-query-v1",
        }
    )


def _validate_cursor_keys(
    keys: object,
    *,
    key_types: Sequence[type[int] | type[str]] | None = None,
) -> tuple[CursorScalar, ...]:
    if not isinstance(keys, list) or not 1 <= len(keys) <= 4:
        raise _invalid_cursor()
    if key_types is not None and len(key_types) != len(keys):
        raise _invalid_cursor()

    validated: list[CursorScalar] = []
    for index, value in enumerate(keys):
        if isinstance(value, bool):
            raise _invalid_cursor()
        if isinstance(value, int):
            if value < _INT64_MIN or value > _INT64_MAX:
                raise _invalid_cursor()
        elif isinstance(value, str):
            byte_length = len(value.encode("utf-8"))
            if not 1 <= byte_length <= 512:
                raise _invalid_cursor()
        else:
            raise _invalid_cursor()
        if key_types is not None and type(value) is not key_types[index]:
            raise _invalid_cursor()
        validated.append(value)
    return tuple(validated)


def encode_cursor(
    keys: Sequence[CursorScalar],
    *,
    query_sha256: str,
) -> str:
    """Encode one canonical, unpadded base64url cursor."""

    if not _SHA256_RE.fullmatch(query_sha256):
        raise ValueError("query_sha256 must be lowercase SHA-256 hex")
    validated_keys = _validate_cursor_keys(list(keys))
    raw = canonical_json(
        {"k": list(validated_keys), "q": query_sha256, "v": _CURSOR_VERSION}
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(encoded.encode("ascii")) > MAX_CURSOR_BYTES:
        raise ValueError("encoded cursor exceeds the maximum size")
    return encoded


def decode_cursor(
    encoded: str,
    *,
    expected_query_sha256: str,
    key_types: Sequence[type[int] | type[str]] | None = None,
) -> tuple[CursorScalar, ...]:
    """Decode a cursor without ever treating failure as the first page."""

    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded.encode("utf-8")) > MAX_CURSOR_BYTES
        or not _BASE64URL_RE.fullmatch(encoded)
        or not _SHA256_RE.fullmatch(expected_query_sha256)
    ):
        raise _invalid_cursor()
    try:
        raw = base64.b64decode(
            encoded + ("=" * (-len(encoded) % 4)),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise _invalid_cursor() from None
    if (
        not isinstance(payload, dict)
        or set(payload) != _CURSOR_FIELDS
        or payload.get("v") != _CURSOR_VERSION
        or payload.get("q") != expected_query_sha256
    ):
        raise _invalid_cursor()
    try:
        canonical = canonical_json(payload).encode("utf-8")
    except ValueError:
        raise _invalid_cursor() from None
    if canonical != raw or base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != encoded:
        raise _invalid_cursor()
    return _validate_cursor_keys(payload.get("k"), key_types=key_types)


@dataclass(frozen=True)
class CursorPage(Generic[ItemT]):
    """A route-neutral page projection using the v2 response field names."""

    items: tuple[ItemT, ...]
    next_cursor: str | None

    def as_response(self) -> dict[str, Any]:
        return {"items": list(self.items), "next_cursor": self.next_cursor}


def build_cursor_page(
    rows: Sequence[ItemT],
    *,
    limit: int,
    query_sha256: str,
    cursor_keys: Callable[[ItemT], Sequence[CursorScalar]],
) -> CursorPage[ItemT]:
    """Project a ``limit + 1`` keyset query into a bounded response page."""

    validated_limit = validate_page_limit(limit)
    if len(rows) > validated_limit + 1:
        raise ValueError("cursor page queries must return at most limit + 1 rows")
    visible = tuple(rows[:validated_limit])
    next_cursor = None
    if len(rows) > validated_limit and visible:
        next_cursor = encode_cursor(
            cursor_keys(visible[-1]),
            query_sha256=query_sha256,
        )
    return CursorPage(items=visible, next_cursor=next_cursor)


__all__ = [
    "CursorPage",
    "CursorScalar",
    "DEFAULT_PAGE_LIMIT",
    "MAX_CURSOR_BYTES",
    "MAX_PAGE_LIMIT",
    "build_cursor_page",
    "decode_cursor",
    "encode_cursor",
    "pagination_query_sha256",
    "validate_page_limit",
]
