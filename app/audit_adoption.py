"""Machine-readable durable-audit adoption inventory.

The migration is intentionally partial.  Keeping the inventory in code makes
that boundary reviewable and lets CI assert that a newly migrated action does
not also call the legacy JSONL writer.  The catalog is additive: unlisted
legacy actions remain best-effort until their own migration slice registers
truthful ownership and tracking metadata.
"""

from __future__ import annotations

from typing import Any, Literal


Durability = Literal["durable", "legacy"]


# Conceptual mutation names are stable across refactors; emitted audit action
# strings are listed separately below because the existing API exposes them.
AUDIT_ADOPTION: dict[str, Durability] = {
    "execution_attempt.create": "durable",
    "node_attempt.create": "durable",
    "project.update": "legacy",
    "approval.decide": "legacy",
}

AUDIT_ADOPTION_METADATA: dict[str, dict[str, str | None]] = {
    "execution_attempt.create": {
        "owner": None,
        "migration_issue": None,
    },
    "node_attempt.create": {
        "owner": None,
        "migration_issue": None,
    },
    "project.update": {
        "owner": None,
        # No issue identifier has been provided in this worktree; keep the
        # gap explicit instead of inventing historical tracking metadata.
        "migration_issue": None,
    },
    "approval.decide": {
        "owner": None,
        "migration_issue": None,
    },
}

# Durable action strings emitted by the migrated UoW paths.  A durable action
# must never be sent to ``append_audit`` at the same call site.
DURABLE_AUDIT_ACTIONS = frozenset(
    {
        "execution_attempt_created",
        "audit_export_replay_requested",
    }
)


def audit_coverage() -> dict[str, Any]:
    """Return the current adoption state for API/operations visibility."""

    durable = sorted(
        action for action, mode in AUDIT_ADOPTION.items() if mode == "durable"
    )
    legacy = sorted(
        action for action, mode in AUDIT_ADOPTION.items() if mode == "legacy"
    )
    return {
        "mode": "full" if not legacy else "partial",
        "durable_actions": durable,
        "legacy_actions": legacy,
        "legacy_without_migration_issue": sorted(
            action
            for action in legacy
            if AUDIT_ADOPTION_METADATA[action].get("migration_issue") is None
        ),
        "entries_without_owner": sorted(
            action
            for action in AUDIT_ADOPTION
            if AUDIT_ADOPTION_METADATA[action].get("owner") is None
        ),
        "catalog_entries": len(AUDIT_ADOPTION),
    }


def adoption_for_action(action: str) -> Durability | None:
    """Map an emitted action string to the catalog when it is known."""

    if action in DURABLE_AUDIT_ACTIONS:
        return "durable"
    if action in {"project_updated", "project_update", "approval_decided", "approve", "reject"}:
        return "legacy"
    return None


__all__ = [
    "AUDIT_ADOPTION",
    "AUDIT_ADOPTION_METADATA",
    "DURABLE_AUDIT_ACTIONS",
    "adoption_for_action",
    "audit_coverage",
]
