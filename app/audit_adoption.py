"""Machine-readable durable-audit adoption inventory.

The inventory is deliberately explicit.  A mutation is either already
transactional or has a named owner and a planned migration slice; an omitted
route is a CI error rather than an implicit legacy exception.  The roadmap
identifier used for legacy entries is a *planned work item*, not fabricated
historical evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


Durability = Literal["durable", "legacy"]


@dataclass(frozen=True)
class AuditAdoptionEntry:
    mutation: str
    emitted_actions: tuple[str, ...]
    durability: Durability
    owner: str
    migration_issue: str | None
    target_slice: str | None


def _legacy(
    mutation: str,
    *actions: str,
    owner: str = "control-plane",
    target_slice: str = "P1-4",
) -> AuditAdoptionEntry:
    return AuditAdoptionEntry(
        mutation=mutation,
        emitted_actions=tuple(actions),
        durability="legacy",
        owner=owner,
        migration_issue="roadmap:P1-4",
        target_slice=target_slice,
    )


def _durable(
    mutation: str,
    *actions: str,
    owner: str = "control-plane",
    target_slice: str | None = None,
) -> AuditAdoptionEntry:
    return AuditAdoptionEntry(
        mutation=mutation,
        emitted_actions=tuple(actions),
        durability="durable",
        owner=owner,
        migration_issue=None,
        target_slice=target_slice,
    )


# Required mutation families from roadmap P1-4.  Keep this list separate from
# the mapping so a missing catalog key is mechanically detectable.
REQUIRED_MUTATIONS = frozenset(
    {
        "identity.service_account",
        "identity.service_token",
        "identity.membership",
        "identity.project_role_binding",
        "identity.session",
        "approval.create",
        "approval.decide",
        "approval.approve",
        "approval.reject",
        "server.add",
        "server.update",
        "server.disable",
        "server.delete",
        "server.bootstrap",
        "server.preflight",
        "node.enroll",
        "node.rotate",
        "node.drain",
        "node.revoke",
        "node.retire",
        "run.create",
        "execution.job_materialize",
        "execution.plan",
        "execution.job_lifecycle",
        "execution_attempt.create",
        "execution_attempt.launch_resolution",
        "execution_attempt.stop",
        "execution_attempt.terminal",
        "execution_attempt.artifact",
        "execution_attempt.result",
        "node_attempt.create",
        "project.create",
        "project.bootstrap",
        "environment.mutate",
        "run_template.mutate",
        "project_defaults.mutate",
        "dataset_asset.mutate",
        "dataset_alias.mutate",
        "dataset_share.mutate",
        "dataset_grant.mutate",
        "project.update",
        "project.delete",
        "project.version",
        "project.candidate",
        "project.instance",
        "project.instance_reconcile",
        "project.remote_mutation",
        "dataset.mutate",
        "dataset.sync_verification",
        "project_snapshot.publish",
        "experiment_record.mutate",
        "engineering_task.mutate",
        "engineering_task.compatibility",
        "engineering_task.retry",
        "engineering_task.discard",
        "engineering_task.cleanup",
        "engineering_task.promote",
        "run_profile.mutate",
        "dispatch_policy.mutate",
    }
)


AUDIT_ADOPTION: dict[str, AuditAdoptionEntry] = {
    "identity.service_account": _durable(
        "identity.service_account", "service_account_created", "service_account_disabled"
    ),
    "identity.service_token": _durable(
        "identity.service_token", "service_token_created", "service_token_revoked"
    ),
    "identity.membership": _durable(
        "identity.membership", "membership_granted", "membership_revoked"
    ),
    # Product v2 role changes reuse the canonical approval and membership
    # events above. Keep this mutation family explicit without claiming those
    # already-owned action strings a second time.
    "identity.project_role_binding": _durable(
        "identity.project_role_binding",
        owner="platform-security",
    ),
    "identity.session": _durable("identity.session", "session_authenticated", "session_revoked"),
    # Identity mutations have durable DB events, but the historical route
    # summaries (including OIDC handshake telemetry) remain compatibility
    # evidence until their JSONL retirement is separately approved.
    "identity.compatibility": _legacy(
        "identity.compatibility",
        "oidc_login_failed",
        "oidc_login_started",
        "oidc_login_succeeded",
        "oidc_logout",
        "service_account_create",
        "service_token_issue",
        "service_token_revoke",
        target_slice="P1-4 literal audit inventory",
    ),
    "approval.create": _durable("approval.create", "approval_created"),
    # Approval creation is durable, but existing request routes still emit
    # the historical JSONL summary for operators and compatibility readers.
    # Track that sink separately instead of implying the summary is a second
    # durable approval event.
    "approval.compatibility": _legacy(
        "approval.compatibility",
        "approval_requested",
        target_slice="P1-4 approval JSONL compatibility",
    ),
    # Request validation and generic compatibility failures still use the
    # append-only JSONL sink. ``reject`` is intentionally not claimed as the
    # durable ``approval_decided`` event: several pre-approval validation
    # paths reject before an approval row exists.
    "control_plane.compatibility": _legacy(
        "control_plane.compatibility",
        "reject",
        target_slice="P1-4 literal audit inventory",
    ),
    # The status transition is now written by ``Database.update_approval`` (or
    # an engineering-specific rejection UoW) in the same transaction as its
    # immutable durable event.  The route-level decision is therefore durable.
    "approval.decide": _durable("approval.decide", "approval_decided"),
    "approval.approve": _durable("approval.approve"),
    # Rejection shares the single ``approval_decided`` event emitted by the
    # decision UoW above; keep the route entry action-empty so the catalog does
    # not claim one immutable action for two mutation families.
    "approval.reject": _durable("approval.reject"),
    # Each route's approval decision is durable through ``approval_decided``.
    # The unpinned YAML compatibility branch still emits the route-specific
    # JSONL summary; those actions are claimed by ``server.compatibility``
    # below rather than making the decision itself look legacy.
    "server.add": _durable("server.add"),
    "server.update": _durable("server.update"),
    "server.disable": _durable("server.disable"),
    "server.delete": _durable("server.delete"),
    "server.compatibility": _legacy(
        "server.compatibility",
        "server_add",
        "server_update",
        "server_disable",
        "server_delete",
        target_slice="P1-4 unpinned server YAML compatibility",
    ),
    "server.operations.compatibility": _legacy(
        "server.operations.compatibility",
        "server_attempt_backend_preflight",
        "server_bootstrap",
        "server_test_ssh",
        "node_enroll",
        "node_rotate",
        "node_revoke",
        "node_retire",
        target_slice="P1-4 literal audit inventory",
    ),
    "server.bootstrap": _durable(
        "server.bootstrap", "server_bootstrap_intent", "server_bootstrap_outcome"
    ),
    "server.preflight": _durable("server.preflight", "server_attempt_backend_preflight_recorded"),
    # Historical unpinned approvals still cannot fabricate a revision, but
    # their external YAML effect is now hash-bound by a durable intent and
    # outcome pair. The JSONL route summary remains separately legacy above.
    "server.legacy_observed": _durable(
        "server.legacy_observed",
        "server_legacy_mutation_intent",
        "server_legacy_mutation_applied",
        "server_legacy_mutation_failed",
    ),
    # Pinned server publication is durable, while the four route entries
    # above remain legacy because unpinned compatibility approvals still
    # exist and are deliberately not backfilled.
    "server.publication": _durable(
        "server.publication",
        "server_add_prepared",
        "server_add_yaml_applied",
        "server_add_rolled_back",
        "server_add_recovery_hold",
        "server_added",
        "server_update_prepared",
        "server_update_yaml_applied",
        "server_update_rolled_back",
        "server_update_recovery_hold",
        "server_updated",
        "server_disable_prepared",
        "server_disable_yaml_applied",
        "server_disable_rolled_back",
        "server_disable_recovery_hold",
        "server_disabled",
        "server_delete_prepared",
        "server_delete_yaml_applied",
        "server_delete_rolled_back",
        "server_delete_recovery_hold",
        "server_deleted",
    ),
    "node.enroll": _durable("node.enroll", "node_enrolled"),
    "node.rotate": _durable("node.rotate", "node_rotated", "node_credential_activated"),
    "node.drain": _durable("node.drain", "node_drained"),
    "node.revoke": _durable("node.revoke", "node_revoked"),
    "node.retire": _durable("node.retire", "node_retired"),
    "run.create": _durable("run.create", "run_created"),
    # Ordinary unpinned enqueue approvals and the standalone compatibility
    # wrapper now publish a bounded materialization event. The wrapper's
    # legacy JSONL line remains explicitly tracked below for compatibility.
    "execution.job_materialize": _durable(
        "execution.job_materialize", "execution_job_materialized"
    ),
    "execution.plan": _durable(
        "execution.plan",
        "execution_plan_materialized",
        "execution_plan_v2_materialized",
        "execution_plan_v2_rejected_stale",
        "execution_plan_v2_job_materialized",
    ),
    "execution.job_lifecycle": _durable(
        "execution.job_lifecycle",
        "execution_job_cancelled",
        "execution_job_dispatched",
        "execution_job_dispatch_requeued",
        "execution_job_terminal_recorded",
        "execution_job_requeued",
        "execution_job_blocked",
        "execution_job_stall_state_recorded",
    ),
    "execution.auto_placement": _durable(
        "execution.auto_placement", "auto_placement_policy_decision"
    ),
    "execution.compatibility": _legacy(
        "execution.compatibility",
        "enqueue",
        "requeue_blocked",
        "auto_placement",
        target_slice="P1-4 execution JSONL compatibility",
    ),
    # Legacy scheduler and plan-request summaries remain best-effort while
    # the attempt-driven path is rolled out.  The durable attempt/job events
    # above are authoritative when that path owns the transition.
    "execution.legacy_compatibility": _legacy(
        "execution.legacy_compatibility",
        "dispatch",
        "dispatch_failed",
        "failed",
        "plan_run_request",
        "plan_run",
        "stop",
        target_slice="P1-4 literal audit inventory",
    ),
    "execution.result_compatibility": _legacy(
        "execution.result_compatibility",
        "job_notified",
        "result_pull_failed",
        "result_pulled",
        target_slice="P1-4 literal audit inventory",
    ),
    "run_profile.mutate": _durable("run_profile.mutate", "run_profile_revision_created"),
    "run_profile.compatibility": _legacy(
        "run_profile.compatibility",
        "run_profile_create",
        "run_profile_update",
        "run_profile_archive",
        target_slice="P1-4 run profile JSONL compatibility",
    ),
    "dispatch_policy.mutate": _durable(
        "dispatch_policy.mutate", "dispatch_policy_revision_created"
    ),
    "dispatch_policy.compatibility": _legacy(
        "dispatch_policy.compatibility",
        "dispatch_policy_create",
        "dispatch_policy_update",
        "dispatch_policy_archive",
        target_slice="P1-4 dispatch policy JSONL compatibility",
    ),
    "execution_attempt.create": _durable("execution_attempt.create", "execution_attempt_created"),
    "execution_attempt.launch_resolution": _durable(
        "execution_attempt.launch_resolution", "launch_resolution_recorded"
    ),
    "execution_attempt.stop": _durable(
        "execution_attempt.stop",
        "execution_stop_requested",
        "execution_stop_acknowledged",
    ),
    "execution_attempt.terminal": _durable(
        "execution_attempt.terminal", "execution_terminal_recorded"
    ),
    "execution_attempt.artifact": _durable(
        "execution_attempt.artifact", "execution_artifact_recorded"
    ),
    "execution_attempt.result": _durable("execution_attempt.result", "execution_result_recorded"),
    "node_attempt.create": _durable("node_attempt.create", "node_attempt_created"),
    "project.create": _durable("project.create", "project_created"),
    "project.bootstrap": _durable(
        "project.bootstrap",
        "project_bootstrap_materialized",
        owner="product-platform",
    ),
    # The same immutable Environment event is emitted both by Project
    # bootstrap and by standalone PR-05 create/update/archive decisions. Its
    # durable ownership therefore belongs to the resource mutation family,
    # while ``project_bootstrap_materialized`` remains the bootstrap boundary.
    "environment.mutate": _durable(
        "environment.mutate",
        "environment_revision_created",
        owner="product-platform",
    ),
    "run_template.mutate": _durable(
        "run_template.mutate",
        "run_profile_spec_created",
        owner="product-platform",
    ),
    "project_defaults.mutate": _durable(
        "project_defaults.mutate",
        "project_defaults_revision_created",
        owner="product-platform",
    ),
    "dataset_asset.mutate": _durable(
        "dataset_asset.mutate",
        "dataset_asset_adopted",
        owner="product-platform",
    ),
    "dataset_alias.mutate": _durable(
        "dataset_alias.mutate",
        "dataset_alias_revision_created",
        owner="product-platform",
    ),
    "dataset_share.mutate": _durable(
        "dataset_share.mutate",
        "dataset_share_offer_created",
        owner="product-platform",
    ),
    "dataset_grant.mutate": _durable(
        "dataset_grant.mutate",
        "dataset_share_grants_created",
        "dataset_grant_withdrawn",
        owner="product-platform",
    ),
    "project.update": _durable("project.update", "project_updated"),
    "project.delete": _durable("project.delete", "project_deleted"),
    "project.version": _durable("project.version", "project_version_created"),
    "project.remote_mutation": _durable(
        "project.remote_mutation",
        "project_git_init_intent",
        "project_git_init_outcome",
        "project_deploy_intent",
        "project_deploy_outcome",
        "project_instance_update_intent",
        "project_instance_update_outcome",
        "project_apply_patch_intent",
        "project_apply_patch_outcome",
        "project_hub_sync_intent",
        "project_hub_sync_outcome",
    ),
    "project.candidate": _durable(
        "project.candidate",
        "project_candidate_created",
        "project_candidate_updated",
        "project_candidate_status_changed",
    ),
    "project.instance": _durable(
        "project.instance",
        "project_instance_created",
        "project_instance_updated",
    ),
    "project.instance_reconcile": _durable(
        "project.instance_reconcile", "project_instance_reconciled"
    ),
    # Older project discovery/import and file-inspection routes still emit
    # their established JSONL summaries.  Remote mutation routes such as
    # hub_sync/git_init/project_deploy/apply_patch now have bounded durable
    # intent/outcome evidence; these entries track only their compatibility
    # summaries, not a second authoritative mutation record.
    "project.compatibility": _legacy(
        "project.compatibility",
        "apply_patch",
        "candidate_manual_added",
        "candidates_ignore_nested",
        "git_init",
        "hub_sync",
        "ignore_project_candidate",
        "import_project",
        "instance_reconcile",
        "inventory_scan",
        "project_activity",
        "project_deploy",
        "project_file_list",
        "project_file_read",
        "project_membership_remove",
        "project_membership_upsert",
        "ignore_nested_candidates",
        target_slice="P1-4 literal audit inventory",
    ),
    "dataset.mutate": _durable(
        "dataset.mutate",
        "dataset_created",
        "dataset_updated",
        "dataset_cache_added",
        "dataset_cache_removed",
        "dataset_cache_reconciled",
    ),
    "dataset.sync_verification": _durable(
        "dataset.sync_verification", "dataset_sync_verification_recorded"
    ),
    "dataset.compatibility": _legacy(
        "dataset.compatibility",
        "cache_reconcile",
        "dataset_prewarm",
        target_slice="P1-4 dataset cache JSONL compatibility",
    ),
    "project_snapshot.publish": _durable("project_snapshot.publish", "project_snapshot_published"),
    "project_snapshot.compatibility": _legacy(
        "project_snapshot.compatibility",
        "dataset_snapshot_build_resume",
        "dataset_snapshot_build",
        target_slice="P1-4 literal audit inventory",
    ),
    "experiment_record.mutate": _durable(
        "experiment_record.mutate",
        "experiment_record_created",
        "experiment_record_updated",
        "experiment_record_deleted",
    ),
    "engineering_task.mutate": _durable(
        "engineering_task.mutate",
        "engineering_task_created",
        "engineering_task_updated",
    ),
    "engineering_task.retry": _durable("engineering_task.retry", "engineering_task_retry_recorded"),
    "engineering_task.discard": _durable("engineering_task.discard", "engineering_task_discarded"),
    "engineering_task.cleanup": _durable(
        "engineering_task.cleanup",
        "engineering_task_cleanup_intent",
        "engineering_task_cleanup_outcome",
    ),
    # The older unbound coding-task wrapper and the cleanup route still emit
    # compatibility summaries; their canonical materialization/cleanup
    # transitions have separate durable UoWs above.
    "engineering_task.compatibility": _legacy(
        "engineering_task.compatibility",
        "coding_task",
        "coding_cleanup",
        "coding_finished",
        "engineering_task_promote",
        "engineering_command",
        "engineering_task_retry",
        "engineering_task_discard",
        target_slice="P1-4 literal audit inventory",
    ),
    # DG-AGENT-SESSION-V1 P1/P2: session open request/decision, the direct
    # close kill-switch, and P2's per-turn launch marker emit legacy JSONL
    # summaries; durable-UoW adoption for the session/turn lifecycle is
    # future work.
    "agent_session.compatibility": _legacy(
        "agent_session.compatibility",
        "agent_session_open",
        "agent_session_close",
        "agent_session_turn_started",
        target_slice="DG-AGENT-SESSION-V1 P3+",
    ),
    "engineering_task.result": _durable(
        "engineering_task.result",
        "engineering_task_result_recorded",
        "coding_run_result_recorded",
    ),
    "engineering_task.promote": _durable(
        "engineering_task.promote",
        "engineering_task_promotion_prepared",
        "engineering_task_promoted",
        "engineering_task_promotion_retired",
    ),
    # The exporter replay already uses the durable UoW path; catalog it even
    # though it is an operational mutation rather than a user route.
    "audit_export.replay": _durable("audit_export.replay", "audit_export_replay_requested"),
    "authorization.compatibility": _legacy(
        "authorization.compatibility",
        "authorization_shadow_denied",
        "authorization_shadow_error",
        target_slice="P1-4 literal audit inventory",
    ),
    "observability.compatibility": _legacy(
        "observability.compatibility",
        "diagnose",
        "stall_notified",
        "stall_suspect",
        target_slice="P1-4 literal audit inventory",
    ),
}


# Emitted durable action strings are kept as a compact compatibility export.
DURABLE_AUDIT_ACTIONS = frozenset(
    action
    for entry in AUDIT_ADOPTION.values()
    if entry.durability == "durable"
    for action in entry.emitted_actions
)

# Compatibility view used by older operational callers.  New code should use
# the typed entries above.
AUDIT_ADOPTION_METADATA: dict[str, dict[str, str | None]] = {
    mutation: {
        "owner": entry.owner,
        "migration_issue": entry.migration_issue,
        "target_slice": entry.target_slice,
    }
    for mutation, entry in AUDIT_ADOPTION.items()
}


def validate_audit_catalog() -> tuple[str, ...]:
    """Return CI-gate violations instead of silently accepting omissions."""

    errors: list[str] = []
    missing = sorted(REQUIRED_MUTATIONS - AUDIT_ADOPTION.keys())
    errors.extend(f"missing mutation catalog entry: {mutation}" for mutation in missing)
    emitted: dict[str, str] = {}
    for mutation, entry in AUDIT_ADOPTION.items():
        if entry.mutation != mutation:
            errors.append(f"catalog key does not match mutation: {mutation}")
        if not entry.owner.strip():
            errors.append(f"catalog entry has no owner: {mutation}")
        if entry.durability == "legacy" and not entry.migration_issue:
            errors.append(f"legacy entry has no migration issue: {mutation}")
        for action in entry.emitted_actions:
            prior = emitted.get(action)
            if prior is not None and prior != mutation:
                errors.append(f"emitted action is claimed by two mutations: {action}")
            emitted[action] = mutation
    return tuple(errors)


def audit_coverage() -> dict[str, Any]:
    """Return the current adoption state for API/operations visibility."""

    durable = sorted(
        mutation for mutation, entry in AUDIT_ADOPTION.items() if entry.durability == "durable"
    )
    legacy = sorted(
        mutation for mutation, entry in AUDIT_ADOPTION.items() if entry.durability == "legacy"
    )
    required_durable = sorted(
        mutation
        for mutation in REQUIRED_MUTATIONS
        if mutation in AUDIT_ADOPTION and AUDIT_ADOPTION[mutation].durability == "durable"
    )
    required_legacy = sorted(
        mutation
        for mutation in REQUIRED_MUTATIONS
        if mutation in AUDIT_ADOPTION and AUDIT_ADOPTION[mutation].durability == "legacy"
    )
    return {
        "mode": "full" if not legacy else "partial",
        "durable_actions": durable,
        "legacy_actions": legacy,
        # Keep the complete catalog views above for compatibility, while
        # exposing the required P1-4 inventory split explicitly. A required
        # legacy entry is an intentional compatibility/operational summary;
        # it must not be mistaken for an untracked mutation family.
        "required_durable_actions": required_durable,
        "required_legacy_actions": required_legacy,
        "required_missing": sorted(REQUIRED_MUTATIONS - AUDIT_ADOPTION.keys()),
        "legacy_without_migration_issue": sorted(
            mutation for mutation in legacy if not AUDIT_ADOPTION[mutation].migration_issue
        ),
        "entries_without_owner": sorted(
            mutation for mutation, entry in AUDIT_ADOPTION.items() if not entry.owner
        ),
        "catalog_entries": len(AUDIT_ADOPTION),
        "catalog_errors": list(validate_audit_catalog()),
    }


def adoption_for_action(action: str) -> Durability | None:
    """Map an emitted action string to the catalog when it is known."""

    for entry in AUDIT_ADOPTION.values():
        if action in entry.emitted_actions:
            return entry.durability
    return None


__all__ = [
    "AUDIT_ADOPTION",
    "AUDIT_ADOPTION_METADATA",
    "AuditAdoptionEntry",
    "DURABLE_AUDIT_ACTIONS",
    "REQUIRED_MUTATIONS",
    "adoption_for_action",
    "audit_coverage",
    "validate_audit_catalog",
]
