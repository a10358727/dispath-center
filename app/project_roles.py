"""Pure Product v2 project-role contracts and readiness evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from app.execution_contract import canonical_json_sha256
from app.identity import (
    Actor,
    ActorType,
    ProjectRoleBinding,
    ProjectRoleGrantProvenance,
    ProjectRoleV2,
)


ROLE_CHANGE_CONTRACT_VERSION = "project-role-change-v1"
ROLE_DIGEST_CONTRACT_VERSION = "project-roles-digest-v1"


class ProjectRBACState(str, Enum):
    READY = "ready"
    NEEDS_ROLE_REPAIR = "needs_role_repair"

    def __str__(self) -> str:
        return self.value


class ProjectRBACReadinessReason(str, Enum):
    OWNER_MISSING = "owner_missing"
    APPROVAL_PAIR_MISSING = "approval_pair_missing"
    ORPHAN_LEGACY_MEMBERSHIP = "orphan_legacy_membership"
    NON_HUMAN_PRIVILEGED_BINDING = "non_human_privileged_binding"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ProjectRBACReadiness:
    state: ProjectRBACState
    reasons: tuple[ProjectRBACReadinessReason, ...]
    enabled_human_owner_count: int
    enabled_human_approval_actor_count: int

    @property
    def ready(self) -> bool:
        return self.state is ProjectRBACState.READY


@dataclass(frozen=True)
class ProjectRoleChange:
    target_actor_id: str
    add_roles: tuple[ProjectRoleV2, ...]
    remove_roles: tuple[ProjectRoleV2, ...]
    expected_roles_digest: str


def normalize_role_change(
    *,
    target_actor_id: str,
    add_roles: Sequence[str | ProjectRoleV2],
    remove_roles: Sequence[str | ProjectRoleV2],
    expected_roles_digest: str,
) -> ProjectRoleChange:
    """Validate one canonical, non-empty role delta."""

    if not isinstance(target_actor_id, str) or not target_actor_id:
        raise ValueError("target_actor_id must be non-empty text")
    if (
        not isinstance(expected_roles_digest, str)
        or len(expected_roles_digest) != 64
        or any(character not in "0123456789abcdef" for character in expected_roles_digest)
    ):
        raise ValueError("expected_roles_digest must be lowercase SHA-256 hex")
    if not isinstance(add_roles, Sequence) or isinstance(add_roles, (str, bytes)):
        raise ValueError("add_roles must be a sequence")
    if not isinstance(remove_roles, Sequence) or isinstance(remove_roles, (str, bytes)):
        raise ValueError("remove_roles must be a sequence")
    normalized_add = tuple(ProjectRoleV2(role) for role in add_roles)
    normalized_remove = tuple(ProjectRoleV2(role) for role in remove_roles)
    if normalized_add != tuple(sorted(set(normalized_add), key=lambda role: role.value)):
        raise ValueError("add_roles must be sorted and unique")
    if normalized_remove != tuple(sorted(set(normalized_remove), key=lambda role: role.value)):
        raise ValueError("remove_roles must be sorted and unique")
    if set(normalized_add) & set(normalized_remove):
        raise ValueError("add_roles and remove_roles must be disjoint")
    if not normalized_add and not normalized_remove:
        raise ValueError("role change must contain at least one role")
    return ProjectRoleChange(
        target_actor_id=target_actor_id,
        add_roles=normalized_add,
        remove_roles=normalized_remove,
        expected_roles_digest=expected_roles_digest,
    )


def active_role_bindings(
    bindings: Iterable[ProjectRoleBinding],
) -> tuple[ProjectRoleBinding, ...]:
    return tuple(binding for binding in bindings if binding.active)


def project_roles_digest(
    bindings: Iterable[ProjectRoleBinding],
    actors: Mapping[str, Actor],
) -> str:
    """Bind every active role identity plus actor type and disabled state."""

    rows = []
    for binding in sorted(
        active_role_bindings(bindings),
        key=lambda item: (item.actor_id, item.role.value, item.id),
    ):
        actor = actors.get(binding.actor_id)
        rows.append(
            {
                "actor_disabled_at": actor.disabled_at if actor is not None else None,
                "actor_id": binding.actor_id,
                "actor_type": actor.actor_type.value if actor is not None else None,
                "binding_id": binding.id,
                "role": binding.role.value,
            }
        )
    return canonical_json_sha256(
        {
            "bindings": rows,
            "contract_version": ROLE_DIGEST_CONTRACT_VERSION,
        }
    )


def evaluate_project_rbac_readiness(
    bindings: Iterable[ProjectRoleBinding],
    actors: Mapping[str, Actor],
    *,
    orphan_legacy_membership: bool = False,
) -> ProjectRBACReadiness:
    """Derive readiness without persisting a mutable eligibility projection."""

    owners: set[str] = set()
    approval_actors: set[str] = set()
    non_human_privileged = False
    for binding in active_role_bindings(bindings):
        if binding.role not in {ProjectRoleV2.OWNER, ProjectRoleV2.REVIEWER}:
            continue
        actor = actors.get(binding.actor_id)
        if actor is None or actor.actor_type is not ActorType.HUMAN:
            non_human_privileged = True
            continue
        if actor.disabled_at is not None:
            continue
        approval_actors.add(actor.id)
        if binding.role is ProjectRoleV2.OWNER:
            owners.add(actor.id)

    reasons: list[ProjectRBACReadinessReason] = []
    if not owners:
        reasons.append(ProjectRBACReadinessReason.OWNER_MISSING)
    if len(approval_actors) < 2:
        reasons.append(ProjectRBACReadinessReason.APPROVAL_PAIR_MISSING)
    if orphan_legacy_membership:
        reasons.append(ProjectRBACReadinessReason.ORPHAN_LEGACY_MEMBERSHIP)
    if non_human_privileged:
        reasons.append(ProjectRBACReadinessReason.NON_HUMAN_PRIVILEGED_BINDING)
    normalized_reasons = tuple(sorted(reasons, key=lambda reason: reason.value))
    return ProjectRBACReadiness(
        state=(
            ProjectRBACState.READY if not normalized_reasons else ProjectRBACState.NEEDS_ROLE_REPAIR
        ),
        reasons=normalized_reasons,
        enabled_human_owner_count=len(owners),
        enabled_human_approval_actor_count=len(approval_actors),
    )


def resulting_bindings_for_role_change(
    bindings: Iterable[ProjectRoleBinding],
    *,
    target_actor_id: str,
    add_roles: Iterable[ProjectRoleV2],
    remove_roles: Iterable[ProjectRoleV2],
) -> tuple[ProjectRoleBinding, ...]:
    """Return an in-memory active-set projection for invariant validation."""

    remove = set(remove_roles)
    projected = [
        binding
        for binding in active_role_bindings(bindings)
        if not (binding.actor_id == target_actor_id and binding.role in remove)
    ]
    existing = {(binding.actor_id, binding.role) for binding in projected}
    for role in add_roles:
        if (target_actor_id, role) in existing:
            raise ValueError("role change adds an already-active role")
        projected.append(
            ProjectRoleBinding(
                id=f"<new:{target_actor_id}:{role.value}>",
                project_id=projected[0].project_id if projected else "<project>",
                actor_id=target_actor_id,
                role=role,
                grant_provenance=ProjectRoleGrantProvenance.APPROVED_ROLE_CHANGE,
                granted_at="<pending>",
            )
        )
    for role in remove:
        if not any(
            binding.actor_id == target_actor_id and binding.role is role
            for binding in active_role_bindings(bindings)
        ):
            raise ValueError("role change removes a role that is not active")
    return tuple(projected)


def validate_resulting_role_change(
    *,
    current_bindings: Iterable[ProjectRoleBinding],
    projected_bindings: Iterable[ProjectRoleBinding],
    actors: Mapping[str, Actor],
    target_actor: Actor,
    add_roles: Iterable[ProjectRoleV2],
    remove_roles: Iterable[ProjectRoleV2],
    orphan_legacy_membership: bool = False,
) -> ProjectRBACReadiness:
    """Enforce role restrictions, ready invariants, and monotonic repair."""

    add = set(add_roles)
    remove = set(remove_roles)
    if target_actor.disabled_at is not None:
        raise ValueError("target actor is disabled")
    if target_actor.actor_type is not ActorType.HUMAN and add & {
        ProjectRoleV2.OWNER,
        ProjectRoleV2.REVIEWER,
    }:
        raise ValueError("non-human actors cannot hold owner or reviewer")

    current = evaluate_project_rbac_readiness(
        current_bindings,
        actors,
        orphan_legacy_membership=orphan_legacy_membership,
    )
    resulting = evaluate_project_rbac_readiness(
        projected_bindings,
        actors,
        orphan_legacy_membership=orphan_legacy_membership,
    )
    if current.ready:
        if not resulting.ready:
            raise ValueError("role change would violate project RBAC readiness")
        return resulting

    if remove:
        raise ValueError("projects needing role repair cannot remove roles")
    if not add or not add <= {ProjectRoleV2.OWNER, ProjectRoleV2.REVIEWER}:
        raise ValueError("project repair may add only owner or reviewer")
    if target_actor.actor_type is not ActorType.HUMAN:
        raise ValueError("project repair requires an enabled human")
    current_metric = (
        current.enabled_human_owner_count,
        current.enabled_human_approval_actor_count,
    )
    resulting_metric = (
        resulting.enabled_human_owner_count,
        resulting.enabled_human_approval_actor_count,
    )
    if not (
        resulting_metric[0] >= current_metric[0]
        and resulting_metric[1] >= current_metric[1]
        and resulting_metric != current_metric
    ):
        raise ValueError("role change does not monotonically repair project RBAC")
    return resulting


__all__ = [
    "ProjectRBACReadiness",
    "ProjectRBACReadinessReason",
    "ProjectRBACState",
    "ProjectRoleChange",
    "ROLE_CHANGE_CONTRACT_VERSION",
    "ROLE_DIGEST_CONTRACT_VERSION",
    "active_role_bindings",
    "evaluate_project_rbac_readiness",
    "normalize_role_change",
    "project_roles_digest",
    "resulting_bindings_for_role_change",
    "validate_resulting_role_change",
]
