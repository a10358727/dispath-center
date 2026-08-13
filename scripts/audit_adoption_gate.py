"""Validate the durable-audit adoption catalog and legacy-writer boundary.

The catalog is intentionally partial, but it must be explicit.  This gate
keeps a newly migrated durable action from silently being sent to the
best-effort JSONL writer, makes missing required mutation families visible in
CI, and prevents a new literal legacy action from bypassing the inventory.
Dynamic legacy action names are not guessed here; they remain covered by the
typed catalog's owner/issue metadata and the domain tests.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from app.audit_adoption import (  # noqa: E402
    DURABLE_AUDIT_ACTIONS,
    adoption_for_action,
    audit_coverage,
    validate_audit_catalog,
)
from app.db import (  # noqa: E402
    TRANSACTION_ONLY_APPROVAL_KINDS,
    VALID_APPROVAL_KINDS,
)


_SERVER_APPROVAL_ACTIONS = frozenset(
    {"server_add", "server_update", "server_disable", "server_delete"}
)
_AUTHORIZATION_SHADOW_ACTIONS = frozenset(
    {"authorization_shadow_denied", "authorization_shadow_error"}
)


def _literal_action_values(expression: ast.AST | None) -> tuple[str, ...]:
    """Return literal action values embedded in a small call-site expression.

    Most call sites pass a constant directly, but the legacy scheduler uses a
    conditional expression for its success/failure summary.  Walking only
    ``node.args[0]`` would silently skip one branch and weaken the inventory
    gate.  Unknown/dynamic expressions intentionally return no values and
    remain a domain-test responsibility.
    """

    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return (expression.value,)
    if isinstance(expression, ast.IfExp):
        return _literal_action_values(expression.body) + _literal_action_values(expression.orelse)
    if isinstance(expression, (ast.Tuple, ast.List, ast.Set)):
        values: list[str] = []
        for element in expression.elts:
            values.extend(_literal_action_values(element))
        return tuple(values)
    return ()


def _dynamic_action_values(expression: ast.AST | None) -> tuple[str, ...] | None:
    """Resolve the closed dynamic action families used by legacy writers.

    A dynamic expression is only safe to inventory when its value set is
    closed by a source-level contract.  Approval kinds come from the DB's
    closed ``VALID_APPROVAL_KINDS`` set; server removal actions and shadow
    evidence actions each have a local closed set.  Returning ``None`` keeps
    genuinely open expressions (for example a caller-supplied ``action``
    parameter) out of the static inference path instead of guessing.
    """

    if isinstance(expression, ast.Attribute) and isinstance(expression.value, ast.Name):
        if expression.value.id == "approval" and expression.attr == "kind":
            return tuple(sorted(VALID_APPROVAL_KINDS - TRANSACTION_ONLY_APPROVAL_KINDS))
        if expression.value.id == "item" and expression.attr == "audit_action":
            return tuple(sorted(_AUTHORIZATION_SHADOW_ACTIONS))
    if isinstance(expression, ast.Name) and expression.id == "action_name":
        return tuple(sorted(_SERVER_APPROVAL_ACTIONS))
    return None


def _legacy_writer_violations(
    source_roots: tuple[Path, ...] | None = None,
) -> list[str]:
    """Find literal durable actions passed to ``append_audit``.

    This is deliberately conservative: only a literal action can be proven
    statically.  A dynamic action still needs a catalog entry and runtime
    tests, while a literal collision is an unambiguous CI failure.
    """

    violations: list[str] = []
    if source_roots is None:
        source_roots = (
            REPOSITORY_ROOT / "app",
            REPOSITORY_ROOT / "dispatch_center",
        )
    for root in source_roots:
        for path in sorted(root.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError as exc:  # pragma: no cover - Ruff/mypy catch this first
                violations.append(f"{path}:{exc.lineno}: syntax error")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                is_legacy_writer = isinstance(function, ast.Name) and function.id == "append_audit"
                is_legacy_writer = is_legacy_writer or (
                    isinstance(function, ast.Attribute) and function.attr == "append_audit"
                )
                if not is_legacy_writer:
                    continue
                action = (
                    node.args[0]
                    if node.args
                    else next(
                        (keyword.value for keyword in node.keywords if keyword.arg == "action"),
                        None,
                    )
                )
                literal_actions = _literal_action_values(action)
                dynamic_actions = _dynamic_action_values(action)
                actions = literal_actions or dynamic_actions or ()
                if not actions:
                    # An open dynamic action cannot be classified safely by
                    # the AST gate.  It remains a domain-test responsibility;
                    # closed dynamic families above are checked explicitly.
                    continue
                for literal_action in actions:
                    durability = adoption_for_action(literal_action)
                    if durability is None:
                        violations.append(
                            f"{path}:{node.lineno}: literal legacy action "
                            f"{literal_action!r} is missing from the audit adoption catalog"
                        )
                    elif literal_action in DURABLE_AUDIT_ACTIONS:
                        violations.append(
                            f"{path}:{node.lineno}: durable action {literal_action!r} "
                            "passed to legacy append_audit"
                        )
    return violations


def main() -> int:
    errors = [*validate_audit_catalog(), *_legacy_writer_violations()]
    report = {
        "status": "fail" if errors else "ok",
        "catalog": audit_coverage(),
        "errors": errors,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
