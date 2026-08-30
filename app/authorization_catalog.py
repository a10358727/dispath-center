"""Static authorization metadata for existing routes and tool interfaces.

The catalog is the closed interface inventory used by shadow observation and
the explicit enforcement adapter.  Metadata remains mode-gated, so the
compatibility default cannot change a request, response, mutation, or remote
side effect.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.authorization import Action


@dataclass(frozen=True)
class InterfaceAuthorizationSpec:
    action: Action
    resource_kind: str


def _spec(action: Action, resource_kind: str) -> InterfaceAuthorizationSpec:
    return InterfaceAuthorizationSpec(action=action, resource_kind=resource_kind)


# `GET /` and the `/static` mount are intentionally public compatibility
# surfaces. Generated OpenAPI/Docs routes are framework-owned but remain behind
# the existing authentication middleware when AUTH_TOKEN or OIDC is enabled.
PUBLIC_ROUTE_INTERFACES = {
    ("GET", "/"),
    ("GET", "/auth/login"),
    ("GET", "/auth/callback"),
}

# Goal 3 C2 (INV-NODE-1): the Node Agent channel. These are **not** public and
# **not** actor-authorized — they are a third, stricter classification:
#
# - every request must carry a valid, non-revoked node credential, enforced
#   structurally in `app.main.auth_middleware` by path prefix before the route
#   runs (not by actor authorization policy; node credentials are a separate
#   protocol boundary and cannot inherit human/service actions);
# - a human session, service token, or legacy shared token is **rejected** here;
# - a node credential is rejected on every other path, so no actor action
#   applies and mapping them into `ROUTE_AUTHORIZATION` would be misleading;
# - the whole prefix 404s while `NODE_AGENT_V1_ENABLED` is false.
#
# They are listed explicitly so route drift still fails the coverage test.
NODE_ROUTE_INTERFACES = {
    # Pending token + activation nonce only; ordinary node credentials remain
    # middleware-authenticated on every other route.
    ("POST", "/node-agent/activate"),
    # Read-only version/capability handshake; it remains node-authenticated,
    # never actor-authorized, and performs no lease or audit mutation.
    ("POST", "/node-agent/probe"),
    # DG-NODE-V2 N-2: restart recovery. Authenticated by node credential like
    # every other /node-agent/* route, never by an actor action.
    ("POST", "/node-agent/current-attempt"),
    ("POST", "/node-agent/poll"),
    ("POST", "/node-agent/ack"),
    ("POST", "/node-agent/heartbeat"),
    ("POST", "/node-agent/terminal"),
    ("POST", "/node-agent/stop-ack"),
    ("POST", "/node-agent/artifacts"),
}
FRAMEWORK_ROUTE_INTERFACES = {
    ("Route", "/openapi.json", "openapi", ("GET", "HEAD")),
    ("Route", "/docs", "swagger_ui_html", ("GET", "HEAD")),
    (
        "Route",
        "/docs/oauth2-redirect",
        "swagger_ui_redirect",
        ("GET", "HEAD"),
    ),
    ("Route", "/redoc", "redoc_html", ("GET", "HEAD")),
    ("Mount", "/static", "static", ()),
}


ROUTE_AUTHORIZATION: dict[tuple[str, str], InterfaceAuthorizationSpec] = {
    ("GET", "/api/v2/me"): _spec(Action.IDENTITY_SELF_VIEW, "identity_self"),
    ("GET", "/api/v2/me/sessions"): _spec(
        Action.IDENTITY_SELF_VIEW, "identity_self"
    ),
    ("GET", "/api/v2/workspace"): _spec(
        Action.IDENTITY_SELF_VIEW, "identity_self"
    ),
    ("GET", "/api/v2/approvals"): _spec(
        Action.APPROVAL_VIEW, "approval_collection"
    ),
    ("GET", "/api/v2/approvals/{approval_id}"): _spec(
        Action.APPROVAL_VIEW, "approval"
    ),
    ("POST", "/api/v2/projects/bootstrap-previews"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/api/v2/projects/bootstrap-requests"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("GET", "/api/v2/projects/{project_id}/workspace"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("GET", "/api/v2/projects/{project_id}/environments"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/environment-change-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/api/v2/projects/{project_id}/run-templates"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/run-template-change-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/api/v2/projects/{project_id}/defaults"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/default-change-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/api/v2/projects/{project_id}/datasets"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/dataset-adoption-requests"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/api/v2/projects/{project_id}/dataset-publish-previews"): _spec(
        Action.DATASET_MANAGE, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/dataset-publish-requests"): _spec(
        Action.DATASET_MANAGE, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/run-previews"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/run-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/instance-update-previews"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/instance-update-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/api/v2/runs/compare"): _spec(
        Action.PROJECT_VIEW, "execution_plan"
    ),
    ("GET", "/api/v2/runs/{plan_id}"): _spec(
        Action.PROJECT_VIEW, "execution_plan"
    ),
    ("POST", "/api/v2/runs/{plan_id}/clone-previews"): _spec(
        Action.PROJECT_OPERATE, "execution_plan"
    ),
    ("POST", "/api/v2/runs/{plan_id}/stop-requests"): _spec(
        Action.PROJECT_OPERATE, "execution_plan"
    ),
    ("GET", "/api/v2/runs/{plan_id}/artifacts"): _spec(
        Action.PROJECT_VIEW, "execution_plan"
    ),
    ("POST", "/api/v2/projects/{project_id}/experiment-previews"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/experiment-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/api/v2/experiments"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("GET", "/api/v2/experiments/{experiment_id}"): _spec(
        Action.PROJECT_VIEW, "experiment"
    ),
    ("GET", "/api/v2/dataset-assets/{asset_id}"): _spec(
        Action.PROJECT_VIEW, "dataset_asset_scope"
    ),
    ("GET", "/api/v2/dataset-assets/{asset_id}/lineage"): _spec(
        Action.PROJECT_VIEW, "dataset_asset_scope"
    ),
    ("GET", "/api/v2/dataset-assets/{asset_id}/usage"): _spec(
        Action.PROJECT_VIEW, "dataset_asset_scope"
    ),
    ("GET", "/api/v2/dataset-assets/{asset_id}/storage"): _spec(
        Action.PROJECT_VIEW, "dataset_asset_scope"
    ),
    ("POST", "/api/v2/dataset-assets/{asset_id}/alias-change-requests"): _spec(
        Action.DATASET_MANAGE, "dataset_asset_scope"
    ),
    ("POST", "/api/v2/dataset-assets/{asset_id}/share-offer-requests"): _spec(
        Action.DATASET_SHARE, "dataset_asset"
    ),
    ("POST", "/api/v2/dataset-share-offers/{offer_id}/accept-requests"): _spec(
        Action.PROJECT_ADMIN, "project_target"
    ),
    ("POST", "/api/v2/dataset-grants/{grant_id}/revoke-requests"): _spec(
        Action.DATASET_WITHDRAW, "project"
    ),
    ("GET", "/api/v2/projects/{project_id}/roles"): _spec(
        Action.PROJECT_ROLE_VIEW, "project"
    ),
    ("POST", "/api/v2/projects/{project_id}/role-change-requests"): _spec(
        Action.PROJECT_ROLE_MANAGE, "project"
    ),
    ("POST", "/api/v2/approvals/{approval_id}/decisions"): _spec(
        Action.APPROVAL_DECIDE, "approval"
    ),
    # DG-UI-UNIFICATION v1 U3: thin `/api/v2/jobs` wrappers around the legacy
    # `/jobs` surface. Jobs stay legacy-scope objects (kind=enqueue/kind=stop
    # `Approval`, decided through `POST /approve`/`POST /reject`), so the
    # action/resource_kind metadata below mirrors the legacy `/jobs*`/
    # `/dispatch` entries exactly rather than a Product v2 typed contract.
    ("GET", "/api/v2/jobs"): _spec(Action.PROJECT_VIEW, "job_collection"),
    ("GET", "/api/v2/jobs/{job_id}"): _spec(Action.PROJECT_VIEW, "job"),
    ("GET", "/api/v2/jobs/{job_id}/log"): _spec(Action.PROJECT_VIEW, "job"),
    ("GET", "/api/v2/jobs/{job_id}/results"): _spec(Action.PROJECT_VIEW, "job"),
    ("GET", "/api/v2/jobs/{job_id}/results/{file_path:path}"): _spec(
        Action.PROJECT_VIEW, "job"
    ),
    ("POST", "/api/v2/jobs/{job_id}/cancel"): _spec(Action.PROJECT_OPERATE, "job"),
    ("POST", "/api/v2/jobs/{job_id}/stop-requests"): _spec(
        Action.PROJECT_OPERATE, "job"
    ),
    ("POST", "/api/v2/jobs/{job_id}/diagnose"): _spec(Action.PROJECT_VIEW, "job"),
    ("POST", "/api/v2/dispatch-requests"): _spec(
        Action.PROJECT_OPERATE, "job_request"
    ),
    # DG-UI-UNIFICATION v1 U4: thin `/api/v2` wrappers around the legacy
    # `/servers*`, `/server-config*`, `/inventory/*`, and
    # `/codex-runner/status` surfaces. These stay legacy-scope `platform`
    # objects (server_add/update/disable/delete, inventory_scan,
    # import_project, ignore_project_candidate, ignore_nested_candidates
    # Approvals decided through `POST /approve`/`POST /reject`), so the
    # action/resource_kind metadata below mirrors the legacy entries exactly.
    ("GET", "/api/v2/servers"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/api/v2/servers/idle-summary"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/api/v2/server-configs"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/api/v2/server-configs/{name}"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("POST", "/api/v2/server-configs/test-ssh"): _spec(
        Action.PLATFORM_VIEW, "platform"
    ),
    ("POST", "/api/v2/server-configs/{name}/attempt-preflight"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    #: DG-INFRA-DIRECT-ACTIONS v1 (2026-08-26): add/update/disable are
    #: direct-execute now (see infrastructure_v2.py module docstring), but
    #: they stay `platform.manage`/"platform" -- same authorization
    #: classification as the approval-card creators they replaced.
    ("POST", "/api/v2/server-configs"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/api/v2/server-configs/{name}/update"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/api/v2/server-configs/{name}/disable"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/api/v2/server-configs/delete-requests"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("GET", "/api/v2/inventory/candidates"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("POST", "/api/v2/inventory/candidates"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/api/v2/inventory/scan-requests"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    (
        "POST",
        "/api/v2/inventory/candidates/{candidate_id}/import-requests",
    ): _spec(Action.PLATFORM_MANAGE, "platform"),
    (
        "POST",
        "/api/v2/inventory/candidates/{candidate_id}/ignore-requests",
    ): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/api/v2/inventory/candidates/ignore-nested-requests"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("GET", "/api/v2/codex-runner/status"): _spec(Action.PLATFORM_VIEW, "platform"),
    # DG-ASSISTANT-CLAUDE-TURN v1 C2 (2026-08-26): the AI-providers status
    # panel is the same legacy-scope `platform` object as the codex-runner
    # status wrapper above; the Anthropic API key setter/clearer is the one
    # documented direct-execute UI mutation in this packet (see
    # `dispatch_center.api.routers.ai_providers_v2` module docstring) --
    # still `platform.manage`, same classification as server add/update.
    ("GET", "/api/v2/ai-providers/status"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("POST", "/api/v2/ai-providers/anthropic-key"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("DELETE", "/api/v2/ai-providers/anthropic-key"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    # Packet D2 (assistant/API model selection): same `platform.manage`
    # classification as the Anthropic API key setter above -- these two
    # endpoints atomically rewrite `.env` (`ASSISTANT_CLAUDE_MODEL`/
    # `LLM_MODEL`) via `app.anthropic_key.set_env_var()`.
    ("POST", "/api/v2/ai-providers/assistant-model"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/api/v2/ai-providers/api-model"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    # Packet D3 (usage accounting): read-only aggregate query, same
    # `platform.view` classification as the status panel above.
    ("GET", "/api/v2/ai-providers/usage"): _spec(Action.PLATFORM_VIEW, "platform"),
    # DG-UI-UNIFICATION v1 U5: thin `/api/v2/legacy-projects*` and
    # `/api/v2/legacy-datasets*` wrappers around the legacy `/projects*`/
    # `/datasets*` surfaces (same reasoning as U3/U4 above): mirrors the
    # legacy `/projects*`/`/datasets*` classification exactly.
    ("GET", "/api/v2/legacy-projects"): _spec(
        Action.PROJECT_VIEW, "project_collection"
    ),
    ("POST", "/api/v2/legacy-projects"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("GET", "/api/v2/projects-matrix"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/api/v2/legacy-projects/{name}/detail"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("GET", "/api/v2/legacy-projects/{name}/versions"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("GET", "/api/v2/legacy-projects/{name}/timeline"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("GET", "/api/v2/legacy-projects/{name}/activity"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("PATCH", "/api/v2/legacy-projects/{name}"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("DELETE", "/api/v2/legacy-projects/{name}"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/records"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("PATCH", "/api/v2/legacy-projects/{name}/records/{record_id}"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("DELETE", "/api/v2/legacy-projects/{name}/records/{record_id}"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/git-init-requests"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/hub-sync"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/deploy-requests"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("GET", "/api/v2/legacy-datasets"): _spec(
        Action.PROJECT_VIEW, "dataset_collection"
    ),
    ("POST", "/api/v2/legacy-datasets"): _spec(Action.PLATFORM_MANAGE, "dataset"),
    ("GET", "/api/v2/legacy-datasets/{name}/{version}/card"): _spec(
        Action.PROJECT_VIEW, "dataset"
    ),
    ("PATCH", "/api/v2/legacy-datasets/{name}/{version}/card"): _spec(
        Action.PLATFORM_MANAGE, "dataset"
    ),
    #: DG-UI-UNIFICATION v1 U6a: thin `/api/v2` wrappers around the legacy
    #: `/engineering-tasks*`, `/coding-agents`, `/coding-runs*`, and the
    #: `/projects/{name}/{engineering-tasks,coding-task}-request*` surfaces
    #: -- same action/resource-kind classification as each legacy route
    #: below (see the matching `("GET"|"POST", "/engineering-tasks...")`/
    #: `("...", "/coding-runs...")`/`("POST", "/projects/{name}/...")`
    #: entries elsewhere in this catalog).
    ("GET", "/api/v2/engineering-tasks/capabilities"): _spec(
        Action.PLATFORM_VIEW, "platform"
    ),
    ("GET", "/api/v2/coding-agents"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/api/v2/engineering-tasks"): _spec(
        Action.PROJECT_VIEW, "engineering_task_collection"
    ),
    ("GET", "/api/v2/engineering-tasks/{task_id}"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/api/v2/engineering-tasks/{task_id}/events"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/api/v2/engineering-tasks/{task_id}/commands/{command_id}/log"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/api/v2/engineering-tasks/{task_id}/diff"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/api/v2/engineering-tasks/{task_id}/patch"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("POST", "/api/v2/engineering-tasks/{task_id}/retry-requests"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    ("POST", "/api/v2/engineering-tasks/{task_id}/discard-requests"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    ("POST", "/api/v2/engineering-tasks/{task_id}/promote-requests"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    (
        "POST",
        "/api/v2/engineering-tasks/{task_id}/worker-validation-requests",
    ): _spec(Action.PROJECT_OPERATE, "engineering_task"),
    ("GET", "/api/v2/coding-runs"): _spec(
        Action.PROJECT_VIEW, "coding_run_collection"
    ),
    ("GET", "/api/v2/coding-runs/{coding_run_id}"): _spec(
        Action.PROJECT_VIEW, "coding_run"
    ),
    ("POST", "/api/v2/coding-runs/{coding_run_id}/cleanup"): _spec(
        Action.PROJECT_ADMIN, "coding_run"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/engineering-task-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/coding-task-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    (
        "POST",
        "/api/v2/legacy-projects/{name}/engineering-task-path-policy-coverage",
    ): _spec(Action.PROJECT_VIEW, "project"),
    #: DG-UI-UNIFICATION v1 U6b: thin `/api/v2` wrappers around the legacy
    #: per-project AI conversation and AgentSession Development Session
    #: surfaces -- same action/resource-kind classification as each legacy
    #: route below (see the matching `("GET"|"POST", "/projects/{name}/
    #: conversation...")`/`("...", "/projects/{name}/agent-sessions...")`/
    #: `("...", "/agent-sessions/{session_id}/...")` entries elsewhere in
    #: this catalog).
    ("GET", "/api/v2/legacy-projects/{name}/conversation"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/conversation/messages"): _spec(
        Action.IDENTITY_SELF_VIEW, "dynamic_agent"
    ),
    ("GET", "/api/v2/legacy-projects/{name}/agent-sessions"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/api/v2/legacy-projects/{name}/agent-session-open-requests"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/api/v2/agent-sessions/{session_id}/close"): _spec(
        Action.PROJECT_ADMIN, "agent_session"
    ),
    ("POST", "/api/v2/agent-sessions/{session_id}/messages"): _spec(
        Action.IDENTITY_SELF_VIEW, "dynamic_agent"
    ),
    ("GET", "/api/v2/agent-sessions/{session_id}/transcript"): _spec(
        Action.PROJECT_VIEW, "agent_session"
    ),
    ("GET", "/api/v2/agent-sessions/{session_id}/diff"): _spec(
        Action.PROJECT_VIEW, "agent_session"
    ),
    ("POST", "/api/v2/agent-sessions/{session_id}/checkpoint-requests"): _spec(
        Action.PROJECT_OPERATE, "agent_session"
    ),
    # DG-UI-UNIFICATION v1 U8: thin `/api/v2/events`/`/api/v2/audit` wrappers
    # around the legacy `("GET", "/events")`/`("GET", "/audit")` entries
    # below -- identical classification (same engine, same
    # `Action.AUDIT_VIEW`, same `"audit"` resource kind).
    ("GET", "/api/v2/events"): _spec(Action.AUDIT_VIEW, "audit"),
    ("GET", "/api/v2/audit"): _spec(Action.AUDIT_VIEW, "audit"),
    ("GET", "/auth/me"): _spec(Action.IDENTITY_SELF_VIEW, "identity_self"),
    ("POST", "/auth/logout"): _spec(Action.IDENTITY_SELF_VIEW, "identity_self"),
    ("GET", "/identity/service-accounts"): _spec(
        Action.IDENTITY_MANAGE, "platform"
    ),
    ("POST", "/identity/service-accounts/request"): _spec(
        Action.IDENTITY_MANAGE, "platform"
    ),
    ("POST", "/identity/service-accounts/{actor_id}/tokens/request"): _spec(
        Action.IDENTITY_MANAGE, "platform"
    ),
    ("POST", "/identity/service-tokens/{token_id}/revoke-request"): _spec(
        Action.IDENTITY_MANAGE, "platform"
    ),
    ("GET", "/servers"): _spec(Action.PLATFORM_VIEW, "platform"),
    # Goal 2 Slice 1: read-only capacity observation history, same
    # classification as GET /servers (no SSH, no state change).
    ("GET", "/servers/{name}/observations"): _spec(Action.PLATFORM_VIEW, "platform"),
    # Goal 2 Slice 2: deterministic idle/capacity summary across all servers,
    # same classification (read-only, no SSH, no scheduling effect).
    ("GET", "/servers/idle-summary"): _spec(Action.PLATFORM_VIEW, "platform"),
    # Goal 3 Phase B（DG-B）：bootstrap 請求會（核准後）對外部機器發 SSH，
    # 屬平台級管理；報告列表揭露主機拓撲，同樣平台級。
    ("POST", "/servers/bootstrap-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("GET", "/servers/bootstrap-reports"): _spec(Action.PLATFORM_VIEW, "platform"),
    # Goal 3 C2（INV-NODE-1）：node 身分的登錄/撤銷是平台級管理（核發可執行
    # 工作的憑證）；清單揭露哪些機器有 agent，屬平台級檢視。
    ("POST", "/nodes/enroll-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/nodes/revoke-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/nodes/rotate-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/nodes/retire-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("GET", "/nodes"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/nodes/operations"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("POST", "/projects"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("GET", "/projects"): _spec(Action.PROJECT_VIEW, "project_collection"),
    # The matrix discloses the complete server topology and global candidate
    # counts in addition to project rows, so it is platform-scoped.
    ("GET", "/projects/matrix"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/projects/{name}/instances"): _spec(Action.PROJECT_VIEW, "project"),
    ("GET", "/projects/{name}/versions"): _spec(Action.PROJECT_VIEW, "project"),
    ("GET", "/projects/{name}/activity"): _spec(Action.PROJECT_VIEW, "project"),
    ("GET", "/projects/{name}/detail"): _spec(Action.PROJECT_VIEW, "project"),
    ("GET", "/projects/{name}/memberships"): _spec(
        Action.PROJECT_MEMBERSHIP_MANAGE, "project"
    ),
    ("POST", "/projects/{name}/memberships/request"): _spec(
        Action.PROJECT_MEMBERSHIP_MANAGE, "project"
    ),
    ("POST", "/projects/{name}/memberships/{actor_id}/remove-request"): _spec(
        Action.PROJECT_MEMBERSHIP_MANAGE, "project"
    ),
    ("GET", "/projects/{name}/run-profiles"): _spec(Action.PROJECT_VIEW, "project"),
    ("POST", "/projects/{name}/run-profiles/request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/projects/{name}/run-profiles/{profile_name}/update-request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/projects/{name}/run-profiles/{profile_name}/archive-request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    # DG-CONVERSATION-V1 CV-2a/CV-5: per-project AI conversation. GET follows
    # every other read-only `/projects/{name}/...` route. POST mirrors
    # `POST /agent/chat`'s classification (Action.IDENTITY_SELF_VIEW,
    # "dynamic_agent") — the turn only queries state and may propose a
    # pending approval via the existing tool loop, the same self-view-level
    # authority as the global chat channel, just project-scoped.
    ("GET", "/projects/{name}/conversation"): _spec(Action.PROJECT_VIEW, "project"),
    ("POST", "/projects/{name}/conversation/messages"): _spec(
        Action.IDENTITY_SELF_VIEW, "dynamic_agent"
    ),
    # DG-AGENT-SESSION-V1 (docs/DECISIONS.md 2026-08-24): persistent
    # AgentSession, P1 slice. GET follows every other read-only
    # `/projects/{name}/...` route. The open-request POST creates a pending
    # `agent_session_open` approval — same material-request classification as
    # `POST /projects/{name}/engineering-tasks/request`.
    ("GET", "/projects/{name}/agent-sessions"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/projects/{name}/agent-sessions/open-request"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    # Goal 2 Slice 3: Dispatch Policy v1, same read/write classification as
    # Run Profile v1 (this slice's policy object has zero runtime effect).
    ("GET", "/projects/{name}/dispatch-policies"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/projects/{name}/dispatch-policies/request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/projects/{name}/dispatch-policies/{policy_name}/update-request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/projects/{name}/dispatch-policies/{policy_name}/archive-request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("PATCH", "/projects/{name}"): _spec(Action.PROJECT_ADMIN, "project"),
    ("GET", "/projects/{name}/timeline"): _spec(Action.PROJECT_VIEW, "project"),
    ("POST", "/projects/{name}/records"): _spec(Action.PROJECT_OPERATE, "project"),
    ("PATCH", "/projects/{name}/records/{record_id}"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("DELETE", "/projects/{name}/records/{record_id}"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/projects/{name}/files"): _spec(Action.PROJECT_VIEW, "project"),
    ("GET", "/projects/{name}/file"): _spec(Action.PROJECT_VIEW, "project"),
    ("POST", "/projects/{name}/apply-patch-request"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/projects/{name}/coding-task-request"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/projects/{name}/engineering-tasks/request"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("POST", "/projects/{name}/engineering-tasks/path-policy-coverage"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/projects/{name}/git-init-request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("POST", "/projects/{name}/hub-sync"): _spec(Action.PROJECT_ADMIN, "project"),
    ("POST", "/projects/{name}/deploy-request"): _spec(
        Action.PROJECT_ADMIN, "project"
    ),
    ("DELETE", "/projects/{name}"): _spec(Action.PROJECT_ADMIN, "project"),
    ("GET", "/codex-runner/status"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/execution-control/status"): _spec(
        Action.PLATFORM_VIEW, "platform"
    ),
    ("GET", "/operations/metrics"): _spec(Action.PLATFORM_VIEW, "platform"),
    # RB-SERVER-001 operator recovery surface.  Reading the journal is a
    # platform view; resolving a recovery_hold is a platform administration
    # action and is never exposed as an LLM/MCP tool.
    ("GET", "/server-config/journal"): _spec(Action.PLATFORM_VIEW, "platform"),
    # Phase 6 health surfaces. Deliberately authenticated: making a probe
    # public would require amending INV-APPROVAL-5's exempt set, which is a
    # protected boundary and not something a health check should quietly widen.
    ("GET", "/healthz"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/readyz"): _spec(Action.PLATFORM_VIEW, "platform"),
    # WP-3B. Preview is a pure read but takes a body, so it is a POST; it
    # creates nothing and therefore carries only a view action.  The run
    # request is a material operation and creates a pending approval.
    ("POST", "/projects/{name}/execution-plans/preview"): _spec(
        Action.PROJECT_VIEW, "project"
    ),
    ("POST", "/projects/{name}/runs/request"): _spec(
        Action.PROJECT_OPERATE, "project"
    ),
    ("GET", "/runs/{plan_id}"): _spec(Action.PROJECT_VIEW, "project"),
    ("POST", "/server-config/journal/{mutation_id}/resolve"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    # Goal 3 Phase A A1：唯讀沙箱 preflight（揭露 Runner 能力，平台級檢視）。
    ("GET", "/codex-runner/sandbox-preflight"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/engineering-tasks/capabilities"): _spec(
        Action.PLATFORM_VIEW, "platform"
    ),
    ("GET", "/coding-agents"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/engineering-tasks"): _spec(
        Action.PROJECT_VIEW, "engineering_task_collection"
    ),
    ("GET", "/engineering-tasks/{task_id}"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("POST", "/engineering-tasks/{task_id}/worker-validation-request"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    ("POST", "/engineering-tasks/{task_id}/promote-request"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    ("POST", "/engineering-tasks/{task_id}/retry-request"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    ("POST", "/engineering-tasks/{task_id}/discard-request"): _spec(
        Action.PROJECT_OPERATE, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/worker-validations"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    (
        "GET",
        "/engineering-tasks/{task_id}/worker-validations/{validation_request_id}",
    ): _spec(Action.PROJECT_VIEW, "engineering_task"),
    ("GET", "/engineering-tasks/{task_id}/attempts"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/events"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/commands"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/commands/{command_id}/log"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/artifacts"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/artifacts/{artifact_id}"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/diff"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/engineering-tasks/{task_id}/patch"): _spec(
        Action.PROJECT_VIEW, "engineering_task"
    ),
    ("GET", "/coding-runs"): _spec(Action.PROJECT_VIEW, "coding_run_collection"),
    ("GET", "/coding-runs/{coding_run_id}"): _spec(Action.PROJECT_VIEW, "coding_run"),
    ("POST", "/coding-runs/{coding_run_id}/cleanup"): _spec(
        Action.PROJECT_ADMIN, "coding_run"
    ),
    # DG-AGENT-SESSION-V1: closing a session is a direct kill-switch action
    # (not approval-gated), same administrative classification as the
    # coding-run cleanup endpoint above.
    ("POST", "/agent-sessions/{session_id}/close"): _spec(
        Action.PROJECT_ADMIN, "agent_session"
    ),
    # DG-AGENT-SESSION-V1 P2 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P2):
    # one turn on an already-approved AgentSession. Same self-view-level
    # classification as `POST /projects/{name}/conversation/messages` — the
    # turn only runs a bounded Claude Code process inside the session's own
    # isolated workspace, never a platform tool. Reading the transcript is a
    # material-scoped read, same classification as every other
    # `/agent-sessions/{session_id}/...` read.
    ("POST", "/agent-sessions/{session_id}/messages"): _spec(
        Action.IDENTITY_SELF_VIEW, "dynamic_agent"
    ),
    ("GET", "/agent-sessions/{session_id}/transcript"): _spec(
        Action.PROJECT_VIEW, "agent_session"
    ),
    # DG-AGENT-SESSION-V1 P3 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P3
    # step 1): read-only remote diff of the session worktree, same
    # classification as every other `/agent-sessions/{session_id}/...` read.
    ("GET", "/agent-sessions/{session_id}/diff"): _spec(
        Action.PROJECT_VIEW, "agent_session"
    ),
    # DG-AGENT-SESSION-CHECKPOINT (docs/DECISIONS.md 2026-08-24: A 核准):
    # creates a pending `agent_session_checkpoint` approval for this session
    # — same material-request classification as the `open-request` route
    # above (`Action.PROJECT_OPERATE`), scoped to the session resource like
    # every other `/agent-sessions/{session_id}/...` route.
    ("POST", "/agent-sessions/{session_id}/checkpoint-request"): _spec(
        Action.PROJECT_OPERATE, "agent_session"
    ),
    ("POST", "/inventory/scan"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("GET", "/inventory/candidates"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/inventory/candidates/{candidate_id}"): _spec(
        Action.PLATFORM_VIEW, "platform"
    ),
    ("POST", "/inventory/candidates/{candidate_id}/import-request"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/inventory/candidates/{candidate_id}/ignore-request"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/inventory/candidates/manual"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/inventory/candidates/ignore-nested-request"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("GET", "/server-config"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("GET", "/server-config/{name}"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("POST", "/server-config/test-ssh"): _spec(Action.PLATFORM_VIEW, "platform"),
    ("POST", "/server-config/{name}/attempt-preflight"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/server-config/add-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/server-config/update-request"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/server-config/disable-request"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/server-config/delete-request"): _spec(
        Action.PLATFORM_MANAGE, "platform"
    ),
    ("POST", "/server-config/reload"): _spec(Action.PLATFORM_MANAGE, "platform"),
    ("POST", "/datasets"): _spec(Action.PLATFORM_MANAGE, "dataset"),
    ("GET", "/datasets"): _spec(Action.PROJECT_VIEW, "dataset_collection"),
    ("POST", "/datasets/{name}/{version}/snapshot-request"): _spec(
        Action.PLATFORM_MANAGE, "dataset"
    ),
    ("GET", "/dataset-snapshots"): _spec(
        Action.PROJECT_VIEW, "dataset_collection"
    ),
    ("GET", "/dataset-snapshots/{snapshot_id}"): _spec(
        Action.PROJECT_VIEW, "dataset"
    ),
    ("POST", "/dataset-snapshots/{snapshot_id}/resume"): _spec(
        Action.PLATFORM_MANAGE, "dataset"
    ),
    ("GET", "/datasets/{name}/{version}/card"): _spec(Action.PROJECT_VIEW, "dataset"),
    ("PATCH", "/datasets/{name}/{version}/card"): _spec(
        Action.PLATFORM_MANAGE, "dataset"
    ),
    ("GET", "/jobs"): _spec(Action.PROJECT_VIEW, "job_collection"),
    ("GET", "/jobs/{job_id}"): _spec(Action.PROJECT_VIEW, "job"),
    ("POST", "/jobs"): _spec(Action.PROJECT_OPERATE, "job_request"),
    ("POST", "/dispatch"): _spec(Action.PROJECT_OPERATE, "job_request"),
    ("GET", "/approvals"): _spec(Action.APPROVAL_VIEW, "approval_collection"),
    ("POST", "/approve/{approval_id}"): _spec(Action.APPROVAL_DECIDE, "approval"),
    ("POST", "/reject/{approval_id}"): _spec(Action.APPROVAL_DECIDE, "approval"),
    ("POST", "/jobs/{job_id}/cancel"): _spec(Action.PROJECT_OPERATE, "job"),
    ("POST", "/jobs/{job_id}/stop"): _spec(Action.PROJECT_OPERATE, "job"),
    ("GET", "/jobs/{job_id}/log"): _spec(Action.PROJECT_VIEW, "job"),
    # PERSONAL_PILOT_PLAN.md §6 T2 / D3: read-only results list/download,
    # local Server A filesystem reads only — same action/resource as the log.
    ("GET", "/jobs/{job_id}/results"): _spec(Action.PROJECT_VIEW, "job"),
    ("GET", "/jobs/{job_id}/results/{file_path:path}"): _spec(
        Action.PROJECT_VIEW, "job"
    ),
    # DG-METRICS-CONTRACT v1: read-only metrics-v1 projection, same
    # action/resource as the log and results surfaces above (flag-gated
    # 404 when METRICS_V1_ENABLED is off; app.main._require_metrics_v1_enabled).
    ("GET", "/jobs/{job_id}/metrics"): _spec(Action.PROJECT_VIEW, "job"),
    ("GET", "/events"): _spec(Action.AUDIT_VIEW, "audit"),
    ("GET", "/audit"): _spec(Action.AUDIT_VIEW, "audit"),
    ("POST", "/jobs/{job_id}/diagnose"): _spec(Action.PROJECT_VIEW, "job"),
    ("POST", "/agent/chat"): _spec(Action.IDENTITY_SELF_VIEW, "dynamic_agent"),
    ("GET", "/agent/tools"): _spec(Action.IDENTITY_SELF_VIEW, "agent_catalog"),
    ("POST", "/agent/cmd"): _spec(Action.IDENTITY_SELF_VIEW, "dynamic_agent"),
    ("WEBSOCKET", "/ws"): _spec(Action.IDENTITY_SELF_VIEW, "dynamic_agent"),
}


LOCAL_TOOL_AUTHORIZATION: dict[str, Action] = {
    "status": Action.PLATFORM_VIEW,
    "servers": Action.PLATFORM_VIEW,
    "jobs": Action.PROJECT_VIEW,
    "job_detail": Action.PROJECT_VIEW,
    "job_log": Action.PROJECT_VIEW,
    "approvals": Action.APPROVAL_VIEW,
    "events": Action.AUDIT_VIEW,
    "gpu": Action.PLATFORM_VIEW,
    "vllm_health": Action.PLATFORM_VIEW,
    "get_dataset_card": Action.PROJECT_VIEW,
    "request_enqueue_job": Action.PROJECT_OPERATE,
    "request_stop_job": Action.PROJECT_OPERATE,
    "request_rerun_job": Action.PROJECT_OPERATE,
    "list_project_candidates": Action.PLATFORM_VIEW,
    "get_project_candidate": Action.PLATFORM_VIEW,
    "search_projects": Action.PROJECT_VIEW,
    "get_project_profile": Action.PROJECT_VIEW,
    "list_project_instances": Action.PROJECT_VIEW,
    "get_project_activity": Action.PROJECT_VIEW,
    "list_project_files": Action.PROJECT_VIEW,
    "read_project_file": Action.PROJECT_VIEW,
    "scan_project_inventory": Action.PLATFORM_MANAGE,
    "request_import_project_candidate": Action.PLATFORM_MANAGE,
    "request_ignore_project_candidate": Action.PLATFORM_MANAGE,
    "list_server_configs": Action.PLATFORM_VIEW,
    "get_server_config": Action.PLATFORM_VIEW,
    "test_server_ssh": Action.PLATFORM_VIEW,
    "request_add_server": Action.PLATFORM_MANAGE,
    "request_update_server": Action.PLATFORM_MANAGE,
    "request_disable_server": Action.PLATFORM_MANAGE,
    "search_experiment_timeline": Action.PROJECT_VIEW,
    "add_experiment_record": Action.PROJECT_OPERATE,
    "update_project_doc": Action.PROJECT_ADMIN,
}

LOCAL_TOOL_RESOURCES: dict[str, str] = {
    "status": "platform",
    "servers": "platform",
    "jobs": "job_collection",
    "job_detail": "job",
    "job_log": "job",
    "approvals": "approval_collection",
    "events": "audit",
    "gpu": "platform",
    "vllm_health": "platform",
    "get_dataset_card": "dataset",
    "request_enqueue_job": "job_request",
    "request_stop_job": "job",
    "request_rerun_job": "job",
    "list_project_candidates": "platform",
    "get_project_candidate": "platform",
    "search_projects": "project_collection",
    "get_project_profile": "project",
    "list_project_instances": "project",
    "get_project_activity": "project",
    "list_project_files": "project",
    "read_project_file": "project",
    "scan_project_inventory": "platform",
    "request_import_project_candidate": "platform",
    "request_ignore_project_candidate": "platform",
    "list_server_configs": "platform",
    "get_server_config": "platform",
    "test_server_ssh": "platform",
    "request_add_server": "platform",
    "request_update_server": "platform",
    "request_disable_server": "platform",
    "search_experiment_timeline": "project",
    "add_experiment_record": "project",
    "update_project_doc": "project",
}


MCP_TOOL_AUTHORIZATION: dict[str, Action] = {
    "get_servers": Action.PLATFORM_VIEW,
    "list_jobs": Action.PROJECT_VIEW,
    "get_job": Action.PROJECT_VIEW,
    "get_job_log": Action.PROJECT_VIEW,
    "list_approvals": Action.APPROVAL_VIEW,
    "list_events": Action.AUDIT_VIEW,
    "list_projects": Action.PROJECT_VIEW,
    "list_datasets": Action.PROJECT_VIEW,
    "get_dataset_card": Action.PROJECT_VIEW,
    "list_project_candidates": Action.PLATFORM_VIEW,
    "get_project_candidate": Action.PLATFORM_VIEW,
    "get_project_activity": Action.PROJECT_VIEW,
    "list_project_files": Action.PROJECT_VIEW,
    "read_project_file": Action.PROJECT_VIEW,
    "request_enqueue_job": Action.PROJECT_OPERATE,
    "request_stop_job": Action.PROJECT_OPERATE,
    "request_apply_patch": Action.PROJECT_OPERATE,
    "request_coding_task": Action.PROJECT_OPERATE,
    "get_codex_runner_status": Action.PLATFORM_VIEW,
    "list_coding_runs": Action.PROJECT_VIEW,
    "get_coding_run": Action.PROJECT_VIEW,
    "get_projects_matrix": Action.PLATFORM_VIEW,
    "get_project_timeline": Action.PROJECT_VIEW,
    "add_experiment_record": Action.PROJECT_OPERATE,
    "update_project_doc": Action.PROJECT_ADMIN,
}


# MCP remains an isolated HTTP client.  This catalog proves that each
# informational tool action matches the action selected by the underlying
# Dispatch Center route; it is never sent as an identity or trust header.
MCP_TOOL_ROUTES: dict[str, tuple[str, str]] = {
    "get_servers": ("GET", "/servers"),
    "list_jobs": ("GET", "/jobs"),
    "get_job": ("GET", "/jobs/{job_id}"),
    "get_job_log": ("GET", "/jobs/{job_id}/log"),
    "list_approvals": ("GET", "/approvals"),
    "list_events": ("GET", "/events"),
    "list_projects": ("GET", "/projects"),
    "list_datasets": ("GET", "/datasets"),
    "get_dataset_card": ("GET", "/datasets/{name}/{version}/card"),
    "list_project_candidates": ("GET", "/inventory/candidates"),
    "get_project_candidate": ("GET", "/inventory/candidates/{candidate_id}"),
    "get_project_activity": ("GET", "/projects/{name}/activity"),
    "list_project_files": ("GET", "/projects/{name}/files"),
    "read_project_file": ("GET", "/projects/{name}/file"),
    "request_enqueue_job": ("POST", "/dispatch"),
    "request_stop_job": ("POST", "/jobs/{job_id}/stop"),
    "request_apply_patch": ("POST", "/projects/{name}/apply-patch-request"),
    "request_coding_task": ("POST", "/projects/{name}/coding-task-request"),
    "get_codex_runner_status": ("GET", "/codex-runner/status"),
    "list_coding_runs": ("GET", "/coding-runs"),
    "get_coding_run": ("GET", "/coding-runs/{coding_run_id}"),
    "get_projects_matrix": ("GET", "/projects/matrix"),
    "get_project_timeline": ("GET", "/projects/{name}/timeline"),
    "add_experiment_record": ("POST", "/projects/{name}/records"),
    "update_project_doc": ("PATCH", "/projects/{name}"),
}

#: DG-ASSISTANT-TOOLS v1 T-3 (packet P1a): a per-turn assistant token may only
#: reach the routes that back the MCP bridge tools -- derived, never hand-copied,
#: so the allowlist cannot drift from the tool catalog. Everything else (approve,
#: reject, identity, settings, ...) is refused before routing with 403.
ASSISTANT_TURN_TOKEN_ROUTES: frozenset[tuple[str, str]] = frozenset(MCP_TOOL_ROUTES.values())
