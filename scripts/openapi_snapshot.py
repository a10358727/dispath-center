"""Human-readable OpenAPI surface snapshot (整頓 C2b, DG-CONSOLIDATION-v1 C-1).

Replaces the opaque ``tests/openapi_snapshot.sha256`` digest: the snapshot
is a sorted JSON map of ``"METHOD /path" -> operationId`` plus the component
schema names, so a route added or removed by a change shows up as a readable
diff in review instead of a hash mismatch.

    python scripts/openapi_snapshot.py --check    # exit 1 + diff on drift
    python scripts/openapi_snapshot.py --update   # rewrite the snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = REPOSITORY_ROOT / "tests" / "openapi_snapshot.json"
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head", "trace")


def build_snapshot() -> dict[str, object]:
    sys.path.insert(0, str(REPOSITORY_ROOT))
    from app.main import app  # noqa: PLC0415 - imported lazily; the app boots the composition root

    schema = app.openapi()
    operations: dict[str, str | None] = {}
    for path, path_item in schema["paths"].items():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                operations[f"{method.upper()} {path}"] = operation.get("operationId")
    return {
        "operations": dict(sorted(operations.items())),
        "schemas": sorted(schema.get("components", {}).get("schemas", {})),
    }


def load_snapshot() -> dict[str, object]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _as_mapping(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    if isinstance(value, list):
        return {str(item): None for item in value}
    return {}


def diff_lines(expected: dict[str, object], actual: dict[str, object]) -> list[str]:
    lines: list[str] = []
    for key in ("operations", "schemas"):
        before = _as_mapping(expected.get(key))
        after = _as_mapping(actual.get(key))
        for item in sorted(set(after) - set(before)):
            lines.append(f"+ {key}: {item}")
        for item in sorted(set(before) - set(after)):
            lines.append(f"- {key}: {item}")
        for item in sorted(set(before) & set(after)):
            if before[item] != after[item]:
                lines.append(f"~ {key}: {item}: {before[item]} -> {after[item]}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--update", action="store_true")
    args = parser.parse_args(argv)
    actual = build_snapshot()
    if args.update:
        SNAPSHOT_PATH.write_text(json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {SNAPSHOT_PATH.relative_to(REPOSITORY_ROOT)}")
        return 0
    expected = load_snapshot()
    lines = diff_lines(expected, actual)
    if not lines:
        print("openapi snapshot: PASS")
        return 0
    print("openapi snapshot: DRIFT (review, then `python scripts/openapi_snapshot.py --update`)")
    print("\n".join(lines))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
