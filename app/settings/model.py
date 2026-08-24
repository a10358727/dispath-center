"""Typed, bounded-context settings composed from the legacy compatibility API."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Any, Optional

from pydantic import SecretStr

from app.settings.features import FEATURE_FLAGS
from app.settings.validation import (
    AUTHORIZATION_MODES,
    MAX_OIDC_CLOCK_SKEW_LEEWAY_SEC,
    PROCESS_ROLES,
    validate_api_bind,
    validate_cookie_name,
    validate_oidc_https_url,
)

if TYPE_CHECKING:
    from app.config import AppConfig, ServerConfig


def _secret(value: Optional[str]) -> Optional[SecretStr]:
    return SecretStr(value) if value is not None else None


@dataclass(frozen=True)
class HttpSettings:
    host: str
    port: int
    process_role: str
    v2_enabled: bool
    product_rbac_v2_enabled: bool
    project_bootstrap_v2_enabled: bool
    project_environments_v1_enabled: bool
    run_template_v2_enabled: bool
    run_experience_v2_enabled: bool
    dataset_assets_v2_enabled: bool
    dataset_sharing_v2_enabled: bool

    def validate(self) -> None:
        validate_api_bind(self.host, self.port)
        if self.process_role not in PROCESS_ROLES:
            raise ValueError(
                f"PROCESS_ROLE={self.process_role!r} is invalid; "
                "expected 'all', 'api', 'scheduler', or 'worker'"
            )
        if self.product_rbac_v2_enabled and not self.v2_enabled:
            raise ValueError(
                "PRODUCT_RBAC_V2_ENABLED=true requires API_V2_ENABLED=true"
            )
        if self.project_bootstrap_v2_enabled and not (
            self.v2_enabled and self.product_rbac_v2_enabled
        ):
            raise ValueError(
                "PROJECT_BOOTSTRAP_V2_ENABLED=true requires API_V2_ENABLED=true "
                "and PRODUCT_RBAC_V2_ENABLED=true"
            )
        if self.project_environments_v1_enabled and not (
            self.v2_enabled and self.product_rbac_v2_enabled
        ):
            raise ValueError(
                "PROJECT_ENVIRONMENTS_V1_ENABLED=true requires API_V2_ENABLED=true "
                "and PRODUCT_RBAC_V2_ENABLED=true"
            )
        if self.run_template_v2_enabled and not (
            self.v2_enabled
            and self.product_rbac_v2_enabled
            and self.project_environments_v1_enabled
        ):
            raise ValueError(
                "RUN_TEMPLATE_V2_ENABLED=true requires API_V2_ENABLED=true, "
                "PRODUCT_RBAC_V2_ENABLED=true, and "
                "PROJECT_ENVIRONMENTS_V1_ENABLED=true"
            )
        if self.dataset_assets_v2_enabled and not (
            self.v2_enabled and self.product_rbac_v2_enabled
        ):
            raise ValueError(
                "DATASET_ASSETS_V2_ENABLED=true requires API_V2_ENABLED=true "
                "and PRODUCT_RBAC_V2_ENABLED=true"
            )
        if self.dataset_sharing_v2_enabled and not (
            self.v2_enabled
            and self.product_rbac_v2_enabled
            and self.dataset_assets_v2_enabled
        ):
            raise ValueError(
                "DATASET_SHARING_V2_ENABLED=true requires API_V2_ENABLED=true, "
                "PRODUCT_RBAC_V2_ENABLED=true, and "
                "DATASET_ASSETS_V2_ENABLED=true"
            )
        if self.run_experience_v2_enabled and not (
            self.v2_enabled
            and self.product_rbac_v2_enabled
            and self.project_environments_v1_enabled
            and self.run_template_v2_enabled
            and self.dataset_assets_v2_enabled
        ):
            raise ValueError(
                "RUN_EXPERIENCE_V2_ENABLED=true requires API_V2_ENABLED=true, "
                "PRODUCT_RBAC_V2_ENABLED=true, "
                "PROJECT_ENVIRONMENTS_V1_ENABLED=true, "
                "RUN_TEMPLATE_V2_ENABLED=true, and "
                "DATASET_ASSETS_V2_ENABLED=true"
            )


@dataclass(frozen=True)
class DatabaseSettings:
    path: str
    local_home_dir: str


@dataclass(frozen=True)
class AuthSettings:
    shared_token: Optional[SecretStr]
    legacy_shared_token_enabled: bool
    service_token_auth_enabled: bool
    authorization_mode: str
    allow_high_risk_self_approval: bool
    session_cookie_name: str
    identity_admin_enabled: bool
    web_direct_execute: bool
    auto_approve_rules_path: str

    def validate(self) -> None:
        if self.authorization_mode not in AUTHORIZATION_MODES:
            raise ValueError(
                f"AUTHORIZATION_MODE={self.authorization_mode!r} is invalid; "
                "expected 'off' or 'shadow'"
            )
        validate_cookie_name(self.session_cookie_name, "SESSION_COOKIE_NAME")


@dataclass(frozen=True)
class OIDCSettings:
    enabled: bool
    issuer: Optional[str]
    client_id: Optional[str]
    client_secret: Optional[SecretStr]
    redirect_uri: Optional[str]
    scopes: tuple[str, ...]
    platform_admin_subjects: frozenset[str]
    login_flow_ttl_sec: int
    session_ttl_sec: int
    flow_cookie_name: str
    provider_timeout_sec: float
    clock_skew_leeway_sec: int

    def validate(self, *, session_cookie_name: str) -> None:
        validate_cookie_name(self.flow_cookie_name, "OIDC_FLOW_COOKIE_NAME")
        if session_cookie_name == self.flow_cookie_name:
            raise ValueError(
                "SESSION_COOKIE_NAME and OIDC_FLOW_COOKIE_NAME must be distinct"
            )

        if isinstance(self.scopes, str):
            raise ValueError("OIDC_SCOPES must be a sequence of scope tokens")
        if any(
            not isinstance(scope, str)
            or not scope
            or scope.strip() != scope
            or any(character.isspace() for character in scope)
            for scope in self.scopes
        ):
            raise ValueError("OIDC_SCOPES contains an invalid scope token")
        if "openid" not in self.scopes:
            raise ValueError("OIDC_SCOPES must contain openid")

        if isinstance(self.platform_admin_subjects, str):
            raise ValueError(
                "OIDC_PLATFORM_ADMIN_SUBJECTS must be a collection of exact subjects"
            )
        if any(
            not isinstance(subject, str) or not subject or subject.strip() != subject
            for subject in self.platform_admin_subjects
        ):
            raise ValueError("OIDC_PLATFORM_ADMIN_SUBJECTS contains an invalid subject")

        for value, setting_name in (
            (self.login_flow_ttl_sec, "OIDC_LOGIN_FLOW_TTL_SEC"),
            (self.session_ttl_sec, "OIDC_SESSION_TTL_SEC"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{setting_name} must be greater than zero")
        if (
            isinstance(self.provider_timeout_sec, bool)
            or not isinstance(self.provider_timeout_sec, (int, float))
            or not isfinite(float(self.provider_timeout_sec))
            or self.provider_timeout_sec <= 0
        ):
            raise ValueError("OIDC_PROVIDER_TIMEOUT_SEC must be greater than zero")
        if (
            isinstance(self.clock_skew_leeway_sec, bool)
            or not isinstance(self.clock_skew_leeway_sec, int)
            or self.clock_skew_leeway_sec < 0
            or self.clock_skew_leeway_sec > MAX_OIDC_CLOCK_SKEW_LEEWAY_SEC
        ):
            raise ValueError("OIDC_CLOCK_SKEW_LEEWAY_SEC must be between zero and 300")

        if self.enabled:
            required: dict[str, object] = {
                "OIDC_ISSUER": self.issuer,
                "OIDC_CLIENT_ID": self.client_id,
                "OIDC_CLIENT_SECRET": (
                    self.client_secret.get_secret_value()
                    if self.client_secret is not None
                    else None
                ),
                "OIDC_REDIRECT_URI": self.redirect_uri,
            }
            missing = [
                name
                for name, value in required.items()
                if not isinstance(value, str) or not value.strip()
            ]
            if missing:
                raise ValueError(
                    "OIDC_ENABLED=true requires " + ", ".join(sorted(missing))
                )
            assert isinstance(self.issuer, str)
            assert isinstance(self.redirect_uri, str)
            validate_oidc_https_url(self.issuer, "OIDC_ISSUER")
            validate_oidc_https_url(
                self.redirect_uri,
                "OIDC_REDIRECT_URI",
                callback=True,
            )


@dataclass(frozen=True)
class SSHSettings:
    connect_timeout_sec: int
    command_timeout_sec: int
    max_concurrency: int
    default_idle_load: float
    allow_root: bool
    key_allowed_dirs: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionSettings:
    shadow_enabled: bool
    new_claims_enabled: bool
    reconcile_existing: bool
    outbox_worker_enabled: bool
    ssh_launch_enabled: bool

    def validate(self) -> None:
        if self.new_claims_enabled and (
            not self.reconcile_existing or not self.outbox_worker_enabled
        ):
            raise ValueError(
                "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true requires "
                "EXECUTION_ATTEMPT_RECONCILE_EXISTING=true and "
                "EXECUTION_OUTBOX_WORKER_ENABLED=true"
            )
        if self.ssh_launch_enabled and not self.new_claims_enabled:
            raise ValueError(
                "EXECUTION_ATTEMPT_SSH_LAUNCH_ENABLED=true requires "
                "EXECUTION_ATTEMPT_NEW_CLAIMS_ENABLED=true"
            )


@dataclass(frozen=True)
class SchedulerSettings:
    interval_sec: int
    dispatch_policy_enabled: bool
    auto_placement_proposals_enabled: bool
    auto_placement_interval_sec: int
    auto_placement_cooldown_sec: int
    auto_placement_kill_switch: bool


@dataclass(frozen=True)
class NodeSettings:
    legacy_aggregate_enabled: bool
    protocol_drain_enabled: bool
    new_assignment_enabled: bool
    allow_missing_version: bool
    rotation_overlap_sec: int
    rotation_pending_ttl_sec: int
    lease_ttl_sec: float
    heartbeat_ttl_sec: float
    heartbeat_grace_sec: float
    canary_require_tag: str

    def validate(self, *, execution: ExecutionSettings) -> None:
        if self.new_assignment_enabled and not self.protocol_drain_enabled:
            raise ValueError(
                "NODE_NEW_ASSIGNMENT_ENABLED=true requires "
                "NODE_PROTOCOL_DRAIN_ENABLED=true"
            )
        if (
            self.new_assignment_enabled
            and not self.legacy_aggregate_enabled
            and (
                not execution.reconcile_existing
                or not execution.outbox_worker_enabled
            )
        ):
            raise ValueError(
                "split Node v2 assignment requires "
                "EXECUTION_ATTEMPT_RECONCILE_EXISTING=true and "
                "EXECUTION_OUTBOX_WORKER_ENABLED=true"
            )
        if isinstance(self.rotation_overlap_sec, bool) or not (
            1 <= self.rotation_overlap_sec <= 86400
        ):
            raise ValueError("NODE_ROTATION_OVERLAP_SEC must be between 1 and 86400")
        if isinstance(self.rotation_pending_ttl_sec, bool) or not (
            60 <= self.rotation_pending_ttl_sec <= 604800
        ):
            raise ValueError(
                "NODE_ROTATION_PENDING_TTL_SEC must be between 60 and 604800"
            )
        for value, setting_name, allow_zero in (
            (self.lease_ttl_sec, "NODE_AGENT_LEASE_TTL_SEC", False),
            (self.heartbeat_ttl_sec, "NODE_AGENT_HEARTBEAT_TTL_SEC", False),
            (self.heartbeat_grace_sec, "NODE_AGENT_HEARTBEAT_GRACE_SEC", True),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or (float(value) < 0 if allow_zero else float(value) <= 0)
            ):
                qualifier = "zero or greater" if allow_zero else "greater than zero"
                raise ValueError(f"{setting_name} must be finite and {qualifier}")


@dataclass(frozen=True)
class DatasetSettings:
    snapshot_enabled: bool
    snapshot_publish_enabled: bool
    snapshot_store_root: str
    snapshot_max_bytes: int
    prewarm_enabled: bool
    prewarm_interval_sec: int
    prewarm_cooldown_sec: int
    prewarm_kill_switch: bool
    reconcile_interval_sec: int
    publish_v2_enabled: bool = False
    publish_local_roots: tuple[str, ...] = ()

    def validate(self) -> None:
        if any(
            not isinstance(root, str) or not root or not root.startswith("/")
            for root in self.publish_local_roots
        ):
            raise ValueError(
                "DATASET_PUBLISH_LOCAL_ROOTS entries must be absolute paths"
            )
        if len(self.publish_local_roots) != len(set(self.publish_local_roots)):
            raise ValueError("DATASET_PUBLISH_LOCAL_ROOTS entries must be unique")
        if self.snapshot_publish_enabled and not self.snapshot_enabled:
            raise ValueError(
                "DATASET_SNAPSHOT_PUBLISH_ENABLED=true requires "
                "DATASET_SNAPSHOT_V1_ENABLED=true"
            )
        if (
            isinstance(self.snapshot_max_bytes, bool)
            or not isinstance(self.snapshot_max_bytes, int)
            or self.snapshot_max_bytes < 1
        ):
            raise ValueError("DATASET_SNAPSHOT_MAX_BYTES must be a positive integer")


@dataclass(frozen=True)
class EngineeringSettings:
    task_backend_enabled: bool
    accept_unsandboxed_finalization: bool
    code_promotion_enabled: bool
    controlled_coding_runner_enabled: bool
    claude_code_agent_enabled: bool
    run_profile_enabled: bool
    runner_server: Optional[str]
    runner_servers: tuple[str, ...]
    workspace_root: str
    max_concurrency: int
    runner_reserve: bool
    network_access: bool
    auth_mode: str

    def validate(self) -> None:
        if self.task_backend_enabled and not self.accept_unsandboxed_finalization:
            raise ValueError(
                "ENGINEERING_TASK_BACKEND_V1=true requires "
                "ENGINEERING_TASK_BACKEND_V1_ACCEPT_UNSANDBOXED_FINALIZATION=true "
                "until the D2 finalization sandbox is implemented "
                "(docs/AI_ENGINEERING_DECISION_GATE.md)"
            )


@dataclass(frozen=True)
class LLMSettings:
    anthropic_api_key: Optional[SecretStr]
    model: str
    vllm_base_url: Optional[str]
    vllm_model: Optional[str]
    vllm_api_key: Optional[SecretStr]
    max_tool_steps: int
    max_concurrency: int
    tool_result_max_chars: int
    project_conversation_v1_enabled: bool


@dataclass(frozen=True)
class ObservabilitySettings:
    audit_path: str
    audit_export_worker_enabled: bool
    legacy_audit_jsonl_enabled: bool
    backup_root: Optional[str]
    monitor_interval_sec: int
    server_observations_enabled: bool
    server_observation_retention_days: int
    smtp_host: Optional[str]
    smtp_port: Optional[int]
    smtp_user: Optional[str]
    smtp_password: Optional[SecretStr]
    mail_from: Optional[str]
    mail_to: Optional[str]
    result_pull_timeout_sec: int
    stall_minutes: int

    def validate(self, *, process_role: str) -> None:
        if self.audit_export_worker_enabled and process_role not in {"all", "worker"}:
            raise ValueError(
                "AUDIT_EXPORT_WORKER_ENABLED=true requires PROCESS_ROLE=all or worker"
            )


@dataclass(frozen=True)
class MachineSettings:
    servers: tuple["ServerConfig", ...]
    servers_yaml_path: str
    server_bootstrap_enabled: bool
    project_reconcile_interval_sec: int


@dataclass(frozen=True)
class Settings:
    """One typed composition built from the legacy flat compatibility object.

    ``AppConfig`` remains mutable while routers/workers are extracted. This
    object is a snapshot, not a second source of environment values; callers
    rebuild it from the same compatibility object after an intentional mutation.
    """

    http: HttpSettings
    database: DatabaseSettings
    auth: AuthSettings
    oidc: OIDCSettings
    ssh: SSHSettings
    execution: ExecutionSettings
    scheduler: SchedulerSettings
    node: NodeSettings
    dataset: DatasetSettings
    engineering: EngineeringSettings
    llm: LLMSettings
    observability: ObservabilitySettings
    machines: MachineSettings

    @classmethod
    def from_app_config(cls, config: "AppConfig") -> "Settings":
        return cls(
            http=HttpSettings(
                host=config.api_host,
                port=config.api_port,
                process_role=config.process_role,
                v2_enabled=config.api_v2_enabled,
                product_rbac_v2_enabled=config.product_rbac_v2_enabled,
                project_bootstrap_v2_enabled=(
                    config.project_bootstrap_v2_enabled
                ),
                project_environments_v1_enabled=(
                    config.project_environments_v1_enabled
                ),
                run_template_v2_enabled=config.run_template_v2_enabled,
                run_experience_v2_enabled=config.run_experience_v2_enabled,
                dataset_assets_v2_enabled=config.dataset_assets_v2_enabled,
                dataset_sharing_v2_enabled=config.dataset_sharing_v2_enabled,
            ),
            database=DatabaseSettings(
                path=config.db_path,
                local_home_dir=config.local_home_dir,
            ),
            auth=AuthSettings(
                shared_token=_secret(config.auth_token),
                legacy_shared_token_enabled=config.legacy_shared_token_enabled,
                service_token_auth_enabled=config.service_token_auth_enabled,
                authorization_mode=config.authorization_mode,
                allow_high_risk_self_approval=(
                    config.allow_high_risk_self_approval
                ),
                session_cookie_name=config.session_cookie_name,
                identity_admin_enabled=config.identity_admin_enabled,
                web_direct_execute=config.web_direct_execute,
                auto_approve_rules_path=config.auto_approve_rules_path,
            ),
            oidc=OIDCSettings(
                enabled=config.oidc_enabled,
                issuer=config.oidc_issuer,
                client_id=config.oidc_client_id,
                client_secret=_secret(config.oidc_client_secret),
                redirect_uri=config.oidc_redirect_uri,
                scopes=config.oidc_scopes,
                platform_admin_subjects=config.oidc_platform_admin_subjects,
                login_flow_ttl_sec=config.oidc_login_flow_ttl_sec,
                session_ttl_sec=config.oidc_session_ttl_sec,
                flow_cookie_name=config.oidc_flow_cookie_name,
                provider_timeout_sec=config.oidc_provider_timeout_sec,
                clock_skew_leeway_sec=config.oidc_clock_skew_leeway_sec,
            ),
            ssh=SSHSettings(
                connect_timeout_sec=config.ssh_connect_timeout,
                command_timeout_sec=config.ssh_command_timeout,
                max_concurrency=config.ssh_max_concurrency,
                default_idle_load=config.default_idle_load,
                allow_root=config.allow_root_ssh,
                key_allowed_dirs=tuple(config.ssh_key_allowed_dirs),
            ),
            execution=ExecutionSettings(
                shadow_enabled=config.execution_attempt_shadow_enabled,
                new_claims_enabled=config.execution_attempt_new_claims_enabled,
                reconcile_existing=config.execution_attempt_reconcile_existing,
                outbox_worker_enabled=config.execution_outbox_worker_enabled,
                ssh_launch_enabled=config.execution_attempt_ssh_launch_enabled,
            ),
            scheduler=SchedulerSettings(
                interval_sec=config.scheduler_interval_sec,
                dispatch_policy_enabled=config.dispatch_policy_v1_enabled,
                auto_placement_proposals_enabled=(
                    config.auto_placement_proposals_enabled
                ),
                auto_placement_interval_sec=config.auto_placement_interval_sec,
                auto_placement_cooldown_sec=config.auto_placement_cooldown_sec,
                auto_placement_kill_switch=config.auto_placement_kill_switch,
            ),
            node=NodeSettings(
                legacy_aggregate_enabled=config.node_agent_v1_enabled,
                protocol_drain_enabled=config.node_protocol_drain_enabled,
                new_assignment_enabled=config.node_new_assignment_enabled,
                allow_missing_version=config.node_protocol_allow_missing_version,
                rotation_overlap_sec=config.node_rotation_overlap_sec,
                rotation_pending_ttl_sec=config.node_rotation_pending_ttl_sec,
                lease_ttl_sec=config.node_agent_lease_ttl_sec,
                heartbeat_ttl_sec=config.node_agent_heartbeat_ttl_sec,
                heartbeat_grace_sec=config.node_agent_heartbeat_grace_sec,
                canary_require_tag=config.node_canary_require_tag,
            ),
            dataset=DatasetSettings(
                publish_v2_enabled=config.dataset_publish_v2_enabled,
                publish_local_roots=tuple(config.dataset_publish_local_roots),
                snapshot_enabled=config.dataset_snapshot_v1_enabled,
                snapshot_publish_enabled=config.dataset_snapshot_publish_enabled,
                snapshot_store_root=config.dataset_snapshot_store_root,
                snapshot_max_bytes=config.dataset_snapshot_max_bytes,
                prewarm_enabled=config.dataset_prewarm_v1_enabled,
                prewarm_interval_sec=config.dataset_prewarm_interval_sec,
                prewarm_cooldown_sec=config.dataset_prewarm_cooldown_sec,
                prewarm_kill_switch=config.dataset_prewarm_kill_switch,
                reconcile_interval_sec=config.dataset_reconcile_interval_sec,
            ),
            engineering=EngineeringSettings(
                task_backend_enabled=config.engineering_task_backend_v1,
                accept_unsandboxed_finalization=(
                    config.engineering_task_backend_v1_accept_unsandboxed_finalization
                ),
                code_promotion_enabled=config.code_promotion_v1_enabled,
                controlled_coding_runner_enabled=config.controlled_coding_runner_v1,
                claude_code_agent_enabled=config.claude_code_agent_v1,
                run_profile_enabled=config.run_profile_v1_enabled,
                runner_server=config.codex_runner_server,
                runner_servers=tuple(config.codex_runner_servers),
                workspace_root=config.codex_workspace_root,
                max_concurrency=config.codex_max_concurrency,
                runner_reserve=config.codex_runner_reserve,
                network_access=config.codex_network_access,
                auth_mode=config.codex_auth_mode,
            ),
            llm=LLMSettings(
                anthropic_api_key=_secret(config.anthropic_api_key),
                model=config.llm_model,
                vllm_base_url=config.vllm_base_url,
                vllm_model=config.vllm_model,
                vllm_api_key=_secret(config.vllm_api_key),
                max_tool_steps=config.agent_max_tool_steps,
                max_concurrency=config.agent_max_concurrency,
                tool_result_max_chars=config.agent_tool_result_max_chars,
                project_conversation_v1_enabled=config.project_conversation_v1_enabled,
            ),
            observability=ObservabilitySettings(
                audit_path=config.audit_path,
                audit_export_worker_enabled=config.audit_export_worker_enabled,
                legacy_audit_jsonl_enabled=config.legacy_audit_jsonl_enabled,
                backup_root=config.backup_root,
                monitor_interval_sec=config.monitor_interval_sec,
                server_observations_enabled=config.server_observations_enabled,
                server_observation_retention_days=(
                    config.server_observation_retention_days
                ),
                smtp_host=config.smtp_host,
                smtp_port=config.smtp_port,
                smtp_user=config.smtp_user,
                smtp_password=_secret(config.smtp_pass),
                mail_from=config.mail_from,
                mail_to=config.mail_to,
                result_pull_timeout_sec=config.result_pull_timeout_sec,
                stall_minutes=config.stall_minutes,
            ),
            machines=MachineSettings(
                servers=tuple(config.servers),
                servers_yaml_path=config.servers_yaml_path,
                server_bootstrap_enabled=config.server_bootstrap_v1_enabled,
                project_reconcile_interval_sec=config.project_reconcile_interval_sec,
            ),
        )

    def validate(self) -> None:
        self.http.validate()
        self.node.validate(execution=self.execution)
        self.auth.validate()
        self.oidc.validate(session_cookie_name=self.auth.session_cookie_name)
        self.engineering.validate()
        self.execution.validate()
        self.dataset.validate()
        if self.dataset.publish_v2_enabled and not (
            self.http.v2_enabled
            and self.http.product_rbac_v2_enabled
            and self.http.dataset_assets_v2_enabled
            and self.dataset.snapshot_enabled
            and self.dataset.snapshot_publish_enabled
        ):
            raise ValueError(
                "DATASET_PUBLISH_V2_ENABLED=true requires API_V2_ENABLED=true, "
                "PRODUCT_RBAC_V2_ENABLED=true, DATASET_ASSETS_V2_ENABLED=true, "
                "DATASET_SNAPSHOT_V1_ENABLED=true, and "
                "DATASET_SNAPSHOT_PUBLISH_ENABLED=true"
            )
        self.observability.validate(process_role=self.http.process_role)

    def feature_report(self) -> dict[str, dict[str, Any]]:
        """Return non-secret flag state plus reviewed lifecycle metadata."""

        return {
            spec.key: {
                "value": spec.value_from(self),
                "env": spec.env_name,
                "owner": spec.owner,
                "default": spec.default,
                "dependencies": list(spec.dependencies),
                "incompatible_with": list(spec.incompatible_with),
                "rollout_state": spec.rollout_state,
                "review_by": spec.review_by.isoformat(),
                "retirement_condition": spec.retirement_condition,
                "retirement_date": (
                    spec.retirement_date.isoformat()
                    if spec.retirement_date is not None
                    else None
                ),
                # Keep the old report key for existing operators/consumers.
                "sunset_after": (
                    spec.sunset_after.isoformat()
                    if spec.sunset_after is not None
                    else None
                ),
                "deprecated": spec.deprecated,
                "replacement": spec.replacement,
            }
            for spec in FEATURE_FLAGS
        }

    def safe_summary(self) -> dict[str, Any]:
        """Return a startup-log-safe summary with secret presence only."""

        return {
            "http": {
                "host": self.http.host,
                "port": self.http.port,
                "process_role": self.http.process_role,
                "api_v2_enabled": self.http.v2_enabled,
                "product_rbac_v2_enabled": self.http.product_rbac_v2_enabled,
                "project_bootstrap_v2_enabled": (
                    self.http.project_bootstrap_v2_enabled
                ),
                "project_environments_v1_enabled": (
                    self.http.project_environments_v1_enabled
                ),
                "run_template_v2_enabled": self.http.run_template_v2_enabled,
                "run_experience_v2_enabled": (
                    self.http.run_experience_v2_enabled
                ),
                "dataset_assets_v2_enabled": self.http.dataset_assets_v2_enabled,
                "dataset_sharing_v2_enabled": self.http.dataset_sharing_v2_enabled,
                "dataset_publish_v2_enabled": self.dataset.publish_v2_enabled,
            },
            "database": {"path": self.database.path},
            "audit": {
                "export_worker_enabled": (
                    self.observability.audit_export_worker_enabled
                ),
                "legacy_jsonl_enabled": self.observability.legacy_audit_jsonl_enabled,
            },
            "machines": {
                "configured_servers": len(self.machines.servers),
                "server_bootstrap_enabled": self.machines.server_bootstrap_enabled,
            },
            "auth": {
                "authorization_mode": self.auth.authorization_mode,
                "allow_high_risk_self_approval": (
                    self.auth.allow_high_risk_self_approval
                ),
                "legacy_shared_token_enabled": (
                    self.auth.legacy_shared_token_enabled
                ),
                "service_token_auth_enabled": self.auth.service_token_auth_enabled,
                "oidc_enabled": self.oidc.enabled,
                "identity_admin_enabled": self.auth.identity_admin_enabled,
            },
            "execution": {
                "new_claims_enabled": self.execution.new_claims_enabled,
                "reconcile_existing": self.execution.reconcile_existing,
                "outbox_worker_enabled": self.execution.outbox_worker_enabled,
                "ssh_launch_enabled": self.execution.ssh_launch_enabled,
            },
            "node": {
                "protocol_drain_enabled": self.node.protocol_drain_enabled,
                "new_assignment_enabled": self.node.new_assignment_enabled,
                "canary_tag_configured": bool(self.node.canary_require_tag),
            },
            "feature_flags": {
                spec.key: spec.value_from(self) for spec in FEATURE_FLAGS
            },
            "optional": {
                "anthropic_configured": self.llm.anthropic_api_key is not None,
                "vllm_configured": bool(
                    self.llm.vllm_base_url and self.llm.vllm_model
                ),
                "smtp_configured": bool(
                    self.observability.smtp_host
                    and self.observability.smtp_port
                    and self.observability.mail_from
                    and self.observability.mail_to
                ),
                "codex_runner_configured": bool(
                    self.engineering.runner_server
                    or self.engineering.runner_servers
                ),
            },
            "secrets": {
                "shared_token_configured": self.auth.shared_token is not None,
                "oidc_client_secret_configured": (
                    self.oidc.client_secret is not None
                ),
                "smtp_password_configured": (
                    self.observability.smtp_password is not None
                ),
                "anthropic_api_key_configured": (
                    self.llm.anthropic_api_key is not None
                ),
                "vllm_api_key_configured": self.llm.vllm_api_key is not None,
            },
        }
