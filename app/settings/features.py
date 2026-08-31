"""Reviewed lifecycle metadata for every runtime feature switch.

Removal dates are not invented when a rollout/canary decision has not supplied
one.  Every flag instead has a concrete review date and a retirement condition;
an actual ``sunset_after`` date is added only by an approved decision record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Optional, Union

if TYPE_CHECKING:
    from app.settings.model import Settings


FlagDefault = Union[bool, str]
LIFECYCLE_REVIEW_DATE = date(2027, 2, 3)


@dataclass(frozen=True)
class FeatureFlagSpec:
    key: str
    env_name: str
    group: str
    attribute: str
    owner: str
    default: FlagDefault
    dependencies: tuple[str, ...]
    incompatible_with: tuple[str, ...]
    rollout_state: str
    review_by: date
    retirement_condition: str
    sunset_after: Optional[date] = None
    deprecated: bool = False
    replacement: Optional[str] = None

    @property
    def retirement_date(self) -> Optional[date]:
        """Approved retirement date, when one exists.

        ``sunset_after`` is retained as the persisted/constructor name for
        compatibility with the original metadata model; the roadmap-facing
        alias makes the intent explicit in reports without inventing a date.
        """

        return self.sunset_after

    def value_from(self, settings: "Settings") -> FlagDefault:
        return getattr(getattr(settings, self.group), self.attribute)


def _flag(
    key: str,
    env_name: str,
    group: str,
    attribute: str,
    owner: str,
    default: FlagDefault,
    *,
    dependencies: tuple[str, ...] = (),
    incompatible_with: tuple[str, ...] = (),
    rollout_state: Optional[str] = None,
    retirement_condition: str = "permanent operational control",
    deprecated: bool = False,
    replacement: Optional[str] = None,
) -> FeatureFlagSpec:
    if rollout_state is None:
        if deprecated:
            rollout_state = "deprecated_alias"
        elif default is True:
            rollout_state = "default_on"
        else:
            rollout_state = "default_off"
    return FeatureFlagSpec(
        key=key,
        env_name=env_name,
        group=group,
        attribute=attribute,
        owner=owner,
        default=default,
        dependencies=dependencies,
        incompatible_with=incompatible_with,
        rollout_state=rollout_state,
        review_by=LIFECYCLE_REVIEW_DATE,
        retirement_condition=retirement_condition,
        deprecated=deprecated,
        replacement=replacement,
    )


FEATURE_FLAGS = (
    _flag(
        "api_v2",
        "API_V2_ENABLED",
        "http",
        "v2_enabled",
        "product-platform",
        False,
        retirement_condition=(
            "retire only after the supported product surface and rollback "
            "policy no longer require a v2 availability switch"
        ),
    ),
    _flag(
        "product_rbac_v2",
        "PRODUCT_RBAC_V2_ENABLED",
        "http",
        "product_rbac_v2_enabled",
        "platform-security",
        False,
        dependencies=("api_v2",),
        retirement_condition=(
            "retire only after multi-role authorization no longer needs an "
            "independent package rollback switch"
        ),
    ),
    _flag(
        "project_bootstrap_v2",
        "PROJECT_BOOTSTRAP_V2_ENABLED",
        "http",
        "project_bootstrap_v2_enabled",
        "product-platform",
        False,
        dependencies=("api_v2", "product_rbac_v2"),
        retirement_condition=(
            "retire only after Project bootstrap no longer needs an independent "
            "package rollback switch"
        ),
    ),
    _flag(
        "project_environments_v1",
        "PROJECT_ENVIRONMENTS_V1_ENABLED",
        "http",
        "project_environments_v1_enabled",
        "product-platform",
        False,
        dependencies=("api_v2", "product_rbac_v2"),
        retirement_condition=(
            "retire only after Host Environment revisions no longer need an "
            "independent package rollback switch"
        ),
    ),
    _flag(
        "run_template_v2",
        "RUN_TEMPLATE_V2_ENABLED",
        "http",
        "run_template_v2_enabled",
        "product-platform",
        False,
        dependencies=(
            "api_v2",
            "product_rbac_v2",
            "project_environments_v1",
        ),
        retirement_condition=(
            "retire only after typed Run Templates and Project Defaults no "
            "longer need an independent package rollback switch"
        ),
    ),
    _flag(
        "run_experience_v2",
        "RUN_EXPERIENCE_V2_ENABLED",
        "http",
        "run_experience_v2_enabled",
        "product-platform",
        False,
        dependencies=(
            "api_v2",
            "product_rbac_v2",
            "project_environments_v1",
            "run_template_v2",
            "dataset_assets_v2",
        ),
        retirement_condition=(
            "retire only after Product ExecutionPlan v2 no longer needs an "
            "independent package rollback switch"
        ),
    ),
    #: DG-EXPERIMENT-V1 EX-7 (docs/DG_EXPERIMENT_V1_DECISION.md, approved
    #: 2026-08-25): one-matrix-one-approval parameter sweeps built on
    #: ExecutionPlan v2, so it can only ever be enabled alongside
    #: `run_experience_v2` (see `HttpSettings.validate()`).
    _flag(
        "experiment_v2",
        "EXPERIMENT_V2_ENABLED",
        "http",
        "experiment_v2_enabled",
        "product-platform",
        False,
        dependencies=("run_experience_v2",),
        retirement_condition=(
            "retire only after Experiment matrices no longer need an "
            "independent package rollback switch"
        ),
    ),
    _flag(
        "dataset_assets_v2",
        "DATASET_ASSETS_V2_ENABLED",
        "http",
        "dataset_assets_v2_enabled",
        "product-platform",
        False,
        dependencies=("api_v2", "product_rbac_v2"),
        retirement_condition=(
            "retire only after Dataset assets, aliases, and lineage no longer "
            "need an independent package rollback switch"
        ),
    ),
    _flag(
        "dataset_sharing_v2",
        "DATASET_SHARING_V2_ENABLED",
        "http",
        "dataset_sharing_v2_enabled",
        "product-platform",
        False,
        dependencies=("api_v2", "product_rbac_v2", "dataset_assets_v2"),
        retirement_condition=(
            "retire only after cross-Project Dataset offers and grants no longer "
            "need an independent package rollback switch"
        ),
    ),
    _flag(
        "dataset_publish_v2",
        "DATASET_PUBLISH_V2_ENABLED",
        "dataset",
        "publish_v2_enabled",
        "product-platform",
        False,
        dependencies=(
            "api_v2",
            "product_rbac_v2",
            "dataset_assets_v2",
            "dataset_snapshot",
            "dataset_snapshot_publish",
        ),
        retirement_condition=(
            "retire only after Dataset publishing no longer needs an "
            "independent package rollback switch"
        ),
    ),
    _flag(
        "legacy_shared_token",
        "LEGACY_SHARED_TOKEN_ENABLED",
        "auth",
        "legacy_shared_token_enabled",
        "platform-security",
        True,
        retirement_condition="retire after every supported client uses OIDC or service tokens",
    ),
    _flag(
        "service_token_auth",
        "SERVICE_TOKEN_AUTH_ENABLED",
        "auth",
        "service_token_auth_enabled",
        "platform-security",
        False,
    ),
    _flag(
        "authorization_mode",
        "AUTHORIZATION_MODE",
        "auth",
        "authorization_mode",
        "platform-security",
        "off",
        retirement_condition="retire off/shadow compatibility after enforce rollout evidence",
    ),
    _flag("oidc", "OIDC_ENABLED", "oidc", "enabled", "platform-security", False),
    _flag(
        "identity_admin",
        "IDENTITY_ADMIN_ENABLED",
        "auth",
        "identity_admin_enabled",
        "platform-security",
        False,
        dependencies=("oidc",),
    ),
    _flag(
        "execution_attempt_shadow",
        "EXECUTION_ATTEMPT_SHADOW_ENABLED",
        "execution",
        "shadow_enabled",
        "execution-reliability",
        False,
        retirement_condition="retire after durable attempts own every supported launch",
    ),
    _flag(
        "execution_attempt_reconcile_existing",
        "EXECUTION_ATTEMPT_RECONCILE_EXISTING",
        "execution",
        "reconcile_existing",
        "execution-reliability",
        False,
    ),
    _flag(
        "execution_outbox_worker",
        "EXECUTION_OUTBOX_WORKER_ENABLED",
        "execution",
        "outbox_worker_enabled",
        "execution-reliability",
        False,
    ),
    _flag(
        "execution_attempt_new_claims",
        "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED",
        "execution",
        "new_claims_enabled",
        "execution-reliability",
        False,
        dependencies=(
            "execution_attempt_reconcile_existing",
            "execution_outbox_worker",
        ),
    ),
    _flag(
        "execution_attempt_ssh_launch",
        "EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED",
        "execution",
        "ssh_launch_enabled",
        "execution-reliability",
        False,
        dependencies=("execution_attempt_new_claims",),
    ),
    _flag(
        "dataset_snapshot",
        "DATASET_SNAPSHOT_V1_ENABLED",
        "dataset",
        "snapshot_enabled",
        "data-platform",
        False,
    ),
    _flag(
        "dataset_snapshot_publish",
        "DATASET_SNAPSHOT_PUBLISH_ENABLED",
        "dataset",
        "snapshot_publish_enabled",
        "data-platform",
        False,
        dependencies=("dataset_snapshot",),
    ),
    _flag(
        "engineering_task_backend",
        "ENGINEERING_TASK_BACKEND_V1",
        "engineering",
        "task_backend_enabled",
        "engineering-platform",
        False,
        dependencies=("engineering_unsandboxed_finalization_ack",),
    ),
    _flag(
        "engineering_unsandboxed_finalization_ack",
        "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION",
        "engineering",
        "accept_unsandboxed_finalization",
        "engineering-platform",
        False,
        retirement_condition="retire when finalization runs inside the approved sandbox",
    ),
    _flag(
        "code_promotion",
        "CODE_PROMOTION_V1_ENABLED",
        "engineering",
        "code_promotion_enabled",
        "engineering-platform",
        False,
    ),
    _flag(
        "agent_session_v1",
        "AGENT_SESSION_V1_ENABLED",
        "engineering",
        "agent_session_v1_enabled",
        "engineering-platform",
        False,
    ),
    _flag(
        "agent_runtime_v3",
        "AGENT_RUNTIME_V3_ENABLED",
        "engineering",
        "agent_runtime_v3_enabled",
        "engineering-platform",
        False,
    ),
    _flag(
        "run_profile",
        "RUN_PROFILE_V1_ENABLED",
        "engineering",
        "run_profile_enabled",
        "engineering-platform",
        False,
    ),
    _flag(
        "dispatch_policy",
        "DISPATCH_POLICY_V1_ENABLED",
        "scheduler",
        "dispatch_policy_enabled",
        "scheduling",
        False,
    ),
    _flag(
        "auto_placement_proposals",
        "AUTO_PLACEMENT_PROPOSALS_ENABLED",
        "scheduler",
        "auto_placement_proposals_enabled",
        "scheduling",
        False,
        dependencies=("dispatch_policy",),
    ),
    _flag(
        "auto_placement_kill_switch",
        "AUTO_PLACEMENT_KILL_SWITCH",
        "scheduler",
        "auto_placement_kill_switch",
        "scheduling",
        True,
        dependencies=("auto_placement_proposals",),
    ),
    _flag(
        "server_observations",
        "SERVER_OBSERVATIONS_ENABLED",
        "observability",
        "server_observations_enabled",
        "operations",
        True,
    ),
    _flag(
        "legacy_audit_jsonl",
        "LEGACY_AUDIT_JSONL_ENABLED",
        "observability",
        "legacy_audit_jsonl_enabled",
        "control-plane",
        True,
        retirement_condition=(
            "retire after all durable compatibility summaries have external "
            "export and operators approve JSONL removal"
        ),
    ),
    _flag(
        "audit_export_worker",
        "AUDIT_EXPORT_WORKER_ENABLED",
        "observability",
        "audit_export_worker_enabled",
        "control-plane",
        False,
        retirement_condition=(
            "retire the compatibility worker only after an approved external "
            "export/retention owner replaces it"
        ),
    ),
    _flag(
        "server_bootstrap",
        "SERVER_BOOTSTRAP_V1_ENABLED",
        "machines",
        "server_bootstrap_enabled",
        "operations",
        False,
    ),
    _flag(
        "dataset_prewarm",
        "DATASET_PREWARM_V1_ENABLED",
        "dataset",
        "prewarm_enabled",
        "data-platform",
        False,
    ),
    _flag(
        "dataset_prewarm_kill_switch",
        "DATASET_PREWARM_KILL_SWITCH",
        "dataset",
        "prewarm_kill_switch",
        "data-platform",
        True,
        dependencies=("dataset_prewarm",),
    ),
    _flag(
        "node_agent_v1_compatibility_alias",
        "NODE_AGENT_V1_ENABLED",
        "node",
        "legacy_aggregate_enabled",
        "node-runtime",
        False,
        retirement_condition="retire after deployments use both split Node controls",
        deprecated=True,
        replacement="NODE_PROTOCOL_DRAIN_ENABLED + NODE_NEW_ASSIGNMENT_ENABLED",
    ),
    _flag(
        "node_protocol_drain",
        "NODE_PROTOCOL_DRAIN_ENABLED",
        "node",
        "protocol_drain_enabled",
        "node-runtime",
        False,
    ),
    _flag(
        "node_new_assignment",
        "NODE_NEW_ASSIGNMENT_ENABLED",
        "node",
        "new_assignment_enabled",
        "node-runtime",
        False,
        dependencies=(
            "node_protocol_drain",
            "execution_attempt_reconcile_existing",
            "execution_outbox_worker",
        ),
    ),
    _flag(
        "allow_root_ssh",
        "ALLOW_ROOT_SSH",
        "ssh",
        "allow_root",
        "platform-security",
        False,
    ),
    _flag(
        "web_direct_execute",
        "WEB_DIRECT_EXECUTE",
        "auth",
        "web_direct_execute",
        "platform-security",
        True,
    ),
    _flag(
        "codex_runner_reserve",
        "CODEX_RUNNER_RESERVE",
        "engineering",
        "runner_reserve",
        "engineering-platform",
        True,
        dependencies=("CODEX_RUNNER_SERVER configured",),
    ),
    _flag(
        "codex_network_access",
        "CODEX_NETWORK_ACCESS",
        "engineering",
        "network_access",
        "platform-security",
        False,
        dependencies=("CODEX_RUNNER_SERVER configured",),
    ),
    _flag(
        "project_conversation",
        "PROJECT_CONVERSATION_V1_ENABLED",
        "llm",
        "project_conversation_v1_enabled",
        "product-platform",
        False,
    ),
    _flag(
        "assistant_tools_v1",
        "ASSISTANT_TOOLS_V1_ENABLED",
        "llm",
        "assistant_tools_v1_enabled",
        "engineering-platform",
        False,
    ),
    _flag(
        "metrics_v1",
        "METRICS_V1_ENABLED",
        "observability",
        "metrics_v1_enabled",
        "product-platform",
        False,
    ),
)

FEATURE_FLAGS_BY_KEY = {spec.key: spec for spec in FEATURE_FLAGS}


def validate_feature_flag_metadata() -> tuple[str, ...]:
    """Return structural metadata errors without evaluating runtime values.

    Dependencies may intentionally name an external prerequisite (for example
    ``CODEX_RUNNER_SERVER configured``), so only references that use a known
    flag key are checked as graph edges. This keeps the registry declarative
    while still preventing typos and self-conflicts from reaching operators.
    """

    errors: list[str] = []
    keys = set(FEATURE_FLAGS_BY_KEY)
    if len(FEATURE_FLAGS_BY_KEY) != len(FEATURE_FLAGS):
        errors.append("duplicate feature flag metadata key")
    for spec in FEATURE_FLAGS:
        if not spec.rollout_state.strip():
            errors.append(f"feature flag has no rollout state: {spec.key}")
        if spec.key in spec.incompatible_with:
            errors.append(f"feature flag conflicts with itself: {spec.key}")
        for other in spec.incompatible_with:
            if other not in keys:
                errors.append(
                    f"feature flag {spec.key} conflicts with unknown flag: {other}"
                )
            elif spec.key not in FEATURE_FLAGS_BY_KEY[other].incompatible_with:
                errors.append(
                    f"feature flag conflict is not symmetric: {spec.key} / {other}"
                )
        for dependency in spec.dependencies:
            if dependency in keys and dependency == spec.key:
                errors.append(f"feature flag depends on itself: {spec.key}")
    return tuple(errors)


if validate_feature_flag_metadata():  # pragma: no cover - import guard
    raise RuntimeError("invalid feature flag metadata")
