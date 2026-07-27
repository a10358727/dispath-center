"""Pure canonical-JSON helpers for DG-EXEC-ATTEMPT-v1 contracts.

The helpers deliberately accept no floats.  Execution digests bind exact
integer/string/boolean/null data and must not inherit platform-dependent float
or NaN serialisation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any


def _validate_canonical_value(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise ValueError(f"execution contract floats are not allowed at {path}")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_canonical_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"execution contract object keys must be strings at {path}"
                )
            _validate_canonical_value(item, f"{path}.{key}")
        return
    raise ValueError(
        f"unsupported execution contract value {type(value).__name__} at {path}"
    )


def canonical_json(value: Any) -> str:
    """Return the approved UTF-8 canonical JSON representation."""

    _validate_canonical_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def utf8_sha256(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("digest input must be text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_execution_contract(value: dict[str, Any]) -> None:
    """Validate the closed WP-1 execution payload shape used before hashing."""

    canonical_json(value)
    authorized_operations = value.get("authorized_operations")
    if (
        not isinstance(authorized_operations, list)
        or not authorized_operations
        or any(
            operation not in {"prepare", "launch", "collect"}
            for operation in authorized_operations
        )
        or len(set(authorized_operations)) != len(authorized_operations)
        or "launch" not in authorized_operations
    ):
        raise ValueError("execution contract has invalid authorized_operations")

    job_specs = value.get("job_specs")
    if not isinstance(job_specs, list) or not job_specs:
        raise ValueError("execution contract requires non-empty job_specs")
    roles: list[str] = []
    dependency_roles: list[str] = []
    for spec in job_specs:
        if not isinstance(spec, dict):
            raise ValueError("execution contract job_specs must contain objects")
        role = spec.get("role")
        if not isinstance(role, str) or not role or role in roles:
            raise ValueError("execution contract job roles must be unique")
        roles.append(role)
        if spec.get("type") not in {"train", "sync", "adhoc", "setup", "coding"}:
            raise ValueError("execution contract job type is invalid")
        encoded = spec.get("command_utf8_b64")
        digest = spec.get("command_sha256")
        if not isinstance(encoded, str) or not isinstance(digest, str):
            raise ValueError("execution contract command bytes and digest are required")
        try:
            command_bytes = base64.b64decode(encoded, validate=True)
            command_bytes.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise ValueError("execution contract command_utf8_b64 is invalid") from None
        if hashlib.sha256(command_bytes).hexdigest() != digest:
            raise ValueError("execution contract command digest mismatch")
        dependencies = spec.get("depends_on_roles", [])
        if (
            not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise ValueError("execution contract dependency roles are invalid")
        dependency_roles.extend(dependencies)
    if any(role not in roles for role in dependency_roles):
        raise ValueError("execution contract references an unknown dependency role")
