(function () {
  "use strict";

  const PRODUCT_READ_PATHS = new Set([
    "/api/v2/me",
    "/api/v2/me/sessions",
    "/api/v2/workspace",
    //: DG-UI-UNIFICATION v1 U3: the job-collection surface has no id
    //: segment, so it is a literal like the three above rather than a regex.
    "/api/v2/jobs",
    //: DG-UI-UNIFICATION v1 U4: thin `/api/v2` wrappers with no id segment
    //: (see `dispatch_center/api/routers/infrastructure_v2.py`).
    "/api/v2/servers",
    "/api/v2/servers/idle-summary",
    "/api/v2/server-configs",
    "/api/v2/inventory/candidates",
    "/api/v2/codex-runner/status",
    //: DG-UI-UNIFICATION v1 U5: thin `/api/v2/legacy-projects*`/
    //: `/api/v2/legacy-datasets*` wrappers with no id segment (see
    //: `dispatch_center/api/routers/projects_legacy_v2.py`).
    "/api/v2/legacy-projects",
    "/api/v2/projects-matrix",
    "/api/v2/legacy-datasets",
    //: DG-UI-UNIFICATION v1 U6a: thin `/api/v2/engineering-tasks*`/
    //: `/api/v2/coding-agents`/`/api/v2/coding-runs` wrappers with no id
    //: segment (see `dispatch_center/api/routers/engineering_v2.py`).
    "/api/v2/engineering-tasks",
    "/api/v2/engineering-tasks/capabilities",
    "/api/v2/coding-agents",
    "/api/v2/coding-runs",
    //: DG-UI-UNIFICATION v1 U6b: the existing U1 `/api/v2/approvals` list
    //: (`kind=` query filter) reused read-only by the checkpoint->promote
    //: bridge poll loops below -- no new backend read surface, same
    //: `?kind=agent_session_checkpoint`/`?kind=engineering_task_promote`
    //: precedent as legacy `static/ui.js`'s `agentSessionCheckpointPollOnce()`/
    //: `agentSessionPromotePollOnce()`.
    "/api/v2/approvals",
  ]);
  const PRODUCT_MUTATION_PATHS = new Set([
    "/api/v2/projects/bootstrap-previews",
    "/api/v2/projects/bootstrap-requests",
    //: DG-UI-UNIFICATION v1 U3: builds a `kind=enqueue` Approval on the
    //: legacy Job/Approval model (see `dispatch_center/api/routers/jobs_v2.py`).
    "/api/v2/dispatch-requests",
    //: DG-UI-UNIFICATION v1 U4: `POST /api/v2/inventory/candidates` is the
    //: **non-approval** manual-candidate-add route (same path as the GET
    //: candidate list above; method disambiguates -- `productRead` never
    //: issues POST, `productMutation` never issues GET).
    "/api/v2/inventory/candidates",
    "/api/v2/inventory/scan-requests",
    "/api/v2/inventory/candidates/ignore-nested-requests",
  ]);
  const PROJECT_WORKSPACE_PATH = /^\/api\/v2\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/workspace$/;
  const DATASET_ASSET_DETAIL_PATH = /^\/api\/v2\/dataset-assets\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const DATASET_PUBLISH_MUTATION_PATH = /^\/api\/v2\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/dataset-publish-(previews|requests)$/;
  const PRODUCT_RUN_CREATE_MUTATION_PATH = /^\/api\/v2\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/run-(previews|requests)$/;
  const INSTANCE_UPDATE_MUTATION_PATH = /^\/api\/v2\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/instance-update-(previews|requests)$/;
  const APPROVAL_DETAIL_PATH = /^\/api\/v2\/approvals\/[1-9][0-9]*$/;
  const APPROVAL_DECISION_PATH = /^\/api\/v2\/approvals\/[1-9][0-9]*\/decisions$/;
  const PRODUCT_RUN_DETAIL_PATH = /^\/api\/v2\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const PRODUCT_RUN_ARTIFACT_PATH = /^\/api\/v2\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/artifacts$/;
  const PRODUCT_RUN_MUTATION_PATH = /^\/api\/v2\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/(clone-previews|stop-requests)$/;
  const PRODUCT_RUN_COMPARE_PATH = "/api/v2/runs/compare";
  //: DG-UI-UNIFICATION v1 U3: thin `/api/v2/jobs` wrapper surfaces (jobs are
  //: legacy-scope objects, integer ids -- not the UUID-keyed Product v2
  //: contracts above). `JOBS_READ_PATH` covers detail/log/results-list/
  //: results-download; `JOBS_MUTATION_PATH` covers cancel/stop-requests/
  //: diagnose. `log?lines=` and any results file path stay reviewed reads
  //: (query strings are never sent through `productMutation`, which forbids
  //: them outright).
  const JOBS_READ_PATH = /^\/api\/v2\/jobs\/[1-9][0-9]*(\/log|\/results(\/.+)?)?$/;
  const JOBS_MUTATION_PATH = /^\/api\/v2\/jobs\/[1-9][0-9]*\/(cancel|stop-requests|diagnose)$/;
  //: DG-UI-UNIFICATION v1 U4: `/api/v2/server-configs/{name}` detail read
  //: (name-keyed, not UUID/int -- server names are the existing
  //: `[A-Za-z0-9_-]+` closed vocabulary enforced server-side by
  //: `app.server_config.validate_server_config()`). `INFRA_SERVER_CONFIG_MUTATION_PATH`
  //: covers test-ssh/add/update/disable/delete-requests;
  //: `INFRA_CANDIDATE_MUTATION_PATH` covers the two per-candidate
  //: import/ignore-request routes.
  const INFRA_SERVER_CONFIG_DETAIL_PATH = /^\/api\/v2\/server-configs\/[^/]+$/;
  const INFRA_SERVER_CONFIG_MUTATION_PATH = /^\/api\/v2\/server-configs\/(test-ssh|add-requests|update-requests|disable-requests|delete-requests)$/;
  const INFRA_CANDIDATE_MUTATION_PATH = /^\/api\/v2\/inventory\/candidates\/[^/]+\/(import-requests|ignore-requests)$/;
  //: DG-UI-UNIFICATION v1 U5: thin `/api/v2/legacy-projects*`/
  //: `/api/v2/legacy-datasets*` wrapper surfaces (name-keyed, not UUID/int
  //: -- see `dispatch_center/api/routers/projects_legacy_v2.py`).
  //: `LEGACY_PROJECT_READ_PATH` covers detail/versions/timeline/activity;
  //: `LEGACY_PROJECT_MUTATION_PATH` covers PATCH/DELETE on the bare project
  //: name; `LEGACY_PROJECT_RECORD_MUTATION_PATH` covers record create
  //: (POST .../records) and update/delete (PATCH/DELETE .../records/{id});
  //: `LEGACY_PROJECT_ACTION_MUTATION_PATH` covers git-init-requests/
  //: hub-sync/deploy-requests. `LEGACY_DATASET_CARD_PATH` is read-only-path-
  //: shaped but reused for both the GET read and the PATCH mutation (same
  //: precedent as `INFRA_SERVER_CONFIG_DETAIL_PATH` vs `_MUTATION_PATH`:
  //: `productRead` never issues PATCH, `productMutation` never issues GET).
  const LEGACY_PROJECT_READ_PATH = /^\/api\/v2\/legacy-projects\/[^/]+\/(detail|versions|timeline|activity)$/;
  const LEGACY_PROJECT_MUTATION_PATH = /^\/api\/v2\/legacy-projects\/[^/]+$/;
  const LEGACY_PROJECT_RECORD_MUTATION_PATH = /^\/api\/v2\/legacy-projects\/[^/]+\/records(\/[1-9][0-9]*)?$/;
  const LEGACY_PROJECT_ACTION_MUTATION_PATH = /^\/api\/v2\/legacy-projects\/[^/]+\/(git-init-requests|hub-sync|deploy-requests)$/;
  const LEGACY_DATASET_CARD_PATH = /^\/api\/v2\/legacy-datasets\/[^/]+\/[^/]+\/card$/;
  //: DG-UI-UNIFICATION v1 U6a: thin `/api/v2/engineering-tasks*`/
  //: `/api/v2/coding-runs*`/`/api/v2/legacy-projects/{name}/{engineering-
  //: task,coding-task}-request*` wrapper surfaces (see
  //: `dispatch_center/api/routers/engineering_v2.py`). `ENGINEERING_TASK_READ_PATH`
  //: covers detail/events/diff/command-log; the `/patch` download route is
  //: deliberately excluded -- it returns a binary/text `text/x-diff` body,
  //: not JSON, so it goes through the dedicated
  //: `authenticatedEngineeringPatchDownload()` below (ported from legacy
  //: `authenticatedDownload()`/`sameOriginDownloadPath()`), never `productRead`.
  //: `ENGINEERING_TASK_MUTATION_PATH` covers retry/discard/promote/worker-
  //: validation requests. `CODING_RUN_READ_PATH`/`CODING_RUN_MUTATION_PATH`
  //: are integer-id-keyed like the legacy `/jobs/{id}` surface.
  //: `LEGACY_PROJECT_ENGINEERING_MUTATION_PATH` covers the three legacy-
  //: project-scoped POST request routes this packet adds.
  const ENGINEERING_TASK_READ_PATH = /^\/api\/v2\/engineering-tasks\/[^/]+(\/events|\/diff|\/commands\/[1-9][0-9]*\/log)?$/;
  const ENGINEERING_TASK_PATCH_PATH = /^\/api\/v2\/engineering-tasks\/[^/]+\/patch$/;
  const ENGINEERING_TASK_MUTATION_PATH = /^\/api\/v2\/engineering-tasks\/[^/]+\/(retry-requests|discard-requests|promote-requests|worker-validation-requests)$/;
  const CODING_RUN_READ_PATH = /^\/api\/v2\/coding-runs\/[1-9][0-9]*$/;
  const CODING_RUN_MUTATION_PATH = /^\/api\/v2\/coding-runs\/[1-9][0-9]*\/cleanup$/;
  const LEGACY_PROJECT_ENGINEERING_MUTATION_PATH = /^\/api\/v2\/legacy-projects\/[^/]+\/(engineering-task-requests|coding-task-requests|engineering-task-path-policy-coverage)$/;
  //: DG-UI-UNIFICATION v1 U6b: thin `/api/v2/legacy-projects/{name}/
  //: {conversation,agent-session*}` and `/api/v2/agent-sessions/{session_id}/
  //: *` wrapper surfaces (see `dispatch_center/api/routers/
  //: projects_legacy_v2.py`). `LEGACY_PROJECT_AI_ENGINEER_READ_PATH` covers
  //: the per-project conversation read and the agent-sessions list;
  //: `LEGACY_PROJECT_AI_ENGINEER_MUTATION_PATH` covers the conversation
  //: message-turn and the agent-session open-request.
  //: `AGENT_SESSION_READ_PATH` covers transcript (with its `?turn=&offset=`
  //: query string) and diff; `AGENT_SESSION_MUTATION_PATH` covers close/
  //: messages/checkpoint-requests -- session ids are UUID-keyed but matched
  //: with `[^/]+`, same convention as `ENGINEERING_TASK_READ_PATH` above.
  const LEGACY_PROJECT_AI_ENGINEER_READ_PATH = /^\/api\/v2\/legacy-projects\/[^/]+\/(conversation|agent-sessions)$/;
  const LEGACY_PROJECT_AI_ENGINEER_MUTATION_PATH = /^\/api\/v2\/legacy-projects\/[^/]+\/(conversation\/messages|agent-session-open-requests)$/;
  const AGENT_SESSION_READ_PATH = /^\/api\/v2\/agent-sessions\/[^/]+\/(transcript|diff)$/;
  const AGENT_SESSION_MUTATION_PATH = /^\/api\/v2\/agent-sessions\/[^/]+\/(close|messages|checkpoint-requests)$/;
  //: DG-UI-UNIFICATION v1 U7 (docs/DECISIONS.md 2026-08-25): the existing
  //: `/ws` chat WebSocket (auth protocol unchanged, see `app/main.py`
  //: `ws_endpoint()`), ported from `static/index.html` `connectChatSocket()`
  //: (:6716-6776). A WebSocket connection is not a `fetch()` call, so it is
  //: outside `PRODUCT_READ_PATHS`/`PRODUCT_MUTATION_PATHS` (the reviewed
  //: fetch allowlist those gate) -- this is instead its own pinned literal,
  //: the single place `new WebSocket(...)` is ever constructed in this file
  //: (see the pinned test asserting exactly one `new WebSocket(` call site).
  const CHAT_WEBSOCKET_PATH = "/ws";
  const DATASET_SHARING_APPROVAL_KINDS = new Set([
    "dataset_share_offer_v2",
    "dataset_share_accept_v2",
    "dataset_grant_revoke_v2",
  ]);
  const REVIEWED_APPROVAL_KINDS = new Set([
    "project_bootstrap_v2",
    "environment_change_v2",
    "run_template_change_v2",
    "project_defaults_change_v2",
    "dataset_alias_change_v2",
    "dataset_publish_v2",
    ...DATASET_SHARING_APPROVAL_KINDS,
    "execution_plan_v2",
    "experiment_create_v2",
    "project_instance_update_v2",
    "stop",
  ]);
  //: DG-UI-UNIFICATION v1 U1 (docs/DECISIONS.md 2026-08-25): every
  //: `VALID_APPROVAL_KINDS` member outside `REVIEWED_APPROVAL_KINDS` (the
  //: typed-contract subset) and `project_role_change` (handled separately
  //: below). These decide through the same compatibility-snapshot digest
  //: flow `enqueue` already used, generalized by the backend generic legacy
  //: decision branch — never a dead end that falls back to the removed
  //: legacy surface.
  const COMPATIBILITY_APPROVAL_KINDS = new Set([
    "enqueue",
    "server_add",
    "server_update",
    "server_disable",
    "server_delete",
    "server_bootstrap",
    "inventory_scan",
    "import_project",
    "ignore_project_candidate",
    "ignore_nested_candidates",
    "apply_patch",
    "coding_task",
    "git_init",
    "project_deploy",
    "service_account_create",
    "service_token_issue",
    "service_token_revoke",
    "project_membership_upsert",
    "project_membership_remove",
    "run_profile_create",
    "run_profile_update",
    "run_profile_archive",
    "dispatch_policy_create",
    "dispatch_policy_update",
    "dispatch_policy_archive",
    "auto_placement",
    "dataset_prewarm",
    "node_enroll",
    "node_revoke",
    "node_rotate",
    "node_retire",
    "plan_run",
    "engineering_task_promote",
    "agent_session_open",
    "agent_session_checkpoint",
    "engineering_task_retry",
    "engineering_task_discard",
    "engineering_command",
    "dataset_snapshot_build",
  ]);
  //: `service_token_issue`/`node_enroll`/`node_rotate` — see
  //: `WorkspaceUI.ONE_TIME_SECRET_APPROVAL_KINDS` (backend source of truth:
  //: `app.db.ONE_TIME_SECRET_APPROVAL_KINDS`). Approve is refused here (and
  //: independently refused server-side) because the successful response
  //: carries a one-time secret this generic review surface has no safe
  //: channel to display; reject stays available.
  const ONE_TIME_SECRET_APPROVAL_KINDS = window.WorkspaceUI.ONE_TIME_SECRET_APPROVAL_KINDS;
  //: DG-UI-UNIFICATION v1 U2 (docs/DECISIONS.md 2026-08-25): full-Chinese
  //: labels for project role badges and capability state pills. Roles are
  //: the canonical `app.db` project role set; unmapped values fall back to
  //: the raw value so an unexpected role never disappears silently.
  const ROLE_LABEL = Object.freeze({
    viewer: "檢視者",
    operator: "操作員",
    admin: "專案管理員",
    reviewer: "審核者",
    dataset_manager: "資料集管理員",
    owner: "擁有者",
  });
  const CAPABILITY_STATE_LABEL = Object.freeze({
    available: "可用",
    disabled: "已停用",
    unavailable: "無法使用",
    execution_plan_product_projection: "ExecutionPlan 投影",
    legacy_job_adapter: "Legacy Job 轉接層",
  });
  function capabilityStateLabel(value) {
    const known = CAPABILITY_STATE_LABEL[value];
    return known || `未知（${value}）`;
  }
  const state = {
    legacyToken: "",
    oidcEnabled: false,
    authenticationModeKnown: false,
    me: null,
    workspace: null,
    sessions: [],
    nextSessionsCursor: null,
    bootstrapPreview: null,
    bootstrapRequestKey: null,
    datasetPublishPreview: null,
    datasetPublishRequestKey: null,
    projectWorkspace: null,
    runCreateProjectId: null,
    runCreateWorkspace: null,
    runCreateAsset: null,
    runCreatePreview: null,
    runCreateRequestKey: null,
    runCreateWorkspaceSerial: 0,
    runCreateAssetSerial: 0,
    runCreatePreviewSerial: 0,
    approvalDetail: null,
    approvalDetailReviewed: false,
    approvalDetailOneTimeSecret: false,
    selectedRunId: null,
    runDetail: null,
    runArtifacts: null,
    runComparison: null,
    runStopRequestKey: null,
    jobs: [],
    jobsLoaded: false,
    openJobLogId: null,
    //: DG-UI-UNIFICATION v1 U4: infrastructure panel (workers/idle-summary/
    //: inventory/codex runner) state.
    infraServerConfigs: [],
    infraServerConfigsLoaded: false,
    infraServers: {},
    infraServerFormMode: "add",
    infraServerFormEditingName: null,
    infraIdleSummary: null,
    infraCandidates: [],
    infraCandidatesLoaded: false,
    infraImportCandidateId: null,
    infraCodexRunnerStatus: null,
    infraCodexRunnerConnectionFailed: false,
    //: DG-UI-UNIFICATION v1 U5: legacy-projects summary (merged onto the
    //: existing Product v2 project cards) + matrix + detail panel + legacy
    //: datasets section state.
    legacyProjectsByName: {},
    legacyProjectsMatrix: null,
    legacyProjectsSummaryLoaded: false,
    legacyProjectDetailName: null,
    legacyProjectDetail: null,
    legacyProjectDetailTab: "overview",
    legacyProjectTimeline: null,
    legacyProjectTimelineKinds: new Set(),
    legacyProjectActivity: null,
    legacyDatasets: [],
    legacyDatasetsLoaded: false,
    legacyDatasetCard: null,
    legacyDatasetCardKey: null,
    //: DG-UI-UNIFICATION v1 U6a: AI 工程（Engineering Task list/detail/
    //: wizard + coding-run cleanup）panel state.
    engineeringTasks: [],
    engineeringTasksLoaded: false,
    engineeringDetailTaskId: null,
    engineeringDetailTask: null,
    engineeringDetailTab: "overview",
    engineeringWizardOpen: false,
    engineeringWizardProviders: [],
    engineeringWizardVersions: [],
    engineeringWizardCapabilities: null,
    engineeringWizardRunnerStatus: null,
    engineeringWizardRunnerConnectionFailed: false,
    engineeringWizardOpenSerial: 0,
    //: DG-UI-UNIFICATION v1 U6b: AgentSession Development Session workbench
    //: (DG-AGENT-SESSION-V1 P4) + per-project AI conversation (DG-
    //: CONVERSATION-V1 CV-2a) state, ported into the U5 project detail tab
    //: bar. `agentSessionLoadSerial` guards panel loads across project
    //: switches; `agentSessionPollSerial`/`agentSessionCheckpointPollSerial`/
    //: `agentSessionPromotePollSerial` each guard their own independent poll
    //: loop, mirroring `static/ui.js`'s `agentSessionState` convention.
    agentSessionLoadSerial: 0,
    agentSessionProjectName: null,
    agentSessionCurrent: null,
    agentSessionPendingApprovalId: null,
    agentSessionReady: false,
    agentSessionSubmittingOpen: false,
    agentSessionSubmittingMessage: false,
    agentSessionClosing: false,
    agentSessionPollSerial: 0,
    agentSessionPollTimer: null,
    agentSessionPollOffset: 0,
    agentSessionDiffLoading: false,
    agentSessionTurnRawEvents: [],
    agentSessionCheckpointApprovalId: null,
    agentSessionCheckpointStatus: null,
    agentSessionCheckpointBridgeTaskId: null,
    agentSessionCheckpointBusy: false,
    agentSessionCheckpointPollSerial: 0,
    agentSessionCheckpointPollTimer: null,
    agentSessionPromoteApprovalId: null,
    agentSessionPromoteStatus: null,
    agentSessionPromoteBusy: false,
    agentSessionPromotePollSerial: 0,
    agentSessionPromotePollTimer: null,
    aiConversationLoadSerial: 0,
    aiConversationProjectName: null,
    aiConversationSubmitting: false,
    aiConversationReady: false,
    //: DG-UI-UNIFICATION v1 U7: 助手（chat assistant）WS client state, ported
    //: from `static/index.html`'s module-level `chatSocket`/
    //: `chatReconnectDelay`/`chatReconnectTimer`/`chatReconnectEnabled`
    //: (:6547-6550). `chatSocketGeneration` is this connection's snapshot of
    //: `state.generation` at connect time (the same identity-refresh guard
    //: legacy used via `authInitializationSerial`) -- a stale socket's
    //: `open`/`message` handlers no-op once `state.generation` has moved on
    //: (logout, session expiry, re-`initialize()`).
    chatSocket: null,
    chatSocketGeneration: -1,
    chatReconnectDelay: 1000,
    chatReconnectTimer: null,
    chatReconnectEnabled: false,
    generation: 0,
  };

  class RequestFailure extends Error {
    constructor(response, body) {
      const apiMessage = body && body.error && body.error.message;
      const legacyMessage = body && body.detail;
      super(apiMessage || legacyMessage || `請求失敗（${response.status}）`);
      this.status = response.status;
      const oidcHeader = response.headers.get("X-OIDC-Enabled");
      this.authenticationModeKnown = oidcHeader !== null;
      this.oidcEnabled = (oidcHeader || "").toLowerCase() === "true";
    }
  }

  function element(id) {
    return document.getElementById(id);
  }

  function node(tag, text, className) {
    const created = document.createElement(tag);
    if (typeof text === "string") created.textContent = text;
    if (className) created.className = className;
    return created;
  }

  function formatTimestamp(value) {
    if (typeof value !== "string" || !value) return "-";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return new Intl.DateTimeFormat("zh-TW", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(parsed);
  }

  function requestHeaders() {
    const headers = { Accept: "application/json" };
    if (state.legacyToken) headers["X-Auth-Token"] = state.legacyToken;
    return headers;
  }

  function randomUUID() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  }

  async function readBody(response) {
    try {
      return await response.json();
    } catch (_error) {
      return null;
    }
  }

  async function productRead(path) {
    const parsed = new URL(path, window.location.origin);
    const reviewedPath = PRODUCT_READ_PATHS.has(parsed.pathname)
      || PROJECT_WORKSPACE_PATH.test(parsed.pathname)
      || DATASET_ASSET_DETAIL_PATH.test(parsed.pathname)
      || APPROVAL_DETAIL_PATH.test(parsed.pathname)
      || PRODUCT_RUN_DETAIL_PATH.test(parsed.pathname)
      || PRODUCT_RUN_ARTIFACT_PATH.test(parsed.pathname)
      || parsed.pathname === PRODUCT_RUN_COMPARE_PATH
      || JOBS_READ_PATH.test(parsed.pathname)
      || INFRA_SERVER_CONFIG_DETAIL_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_READ_PATH.test(parsed.pathname)
      || LEGACY_DATASET_CARD_PATH.test(parsed.pathname)
      || ENGINEERING_TASK_READ_PATH.test(parsed.pathname)
      || CODING_RUN_READ_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_AI_ENGINEER_READ_PATH.test(parsed.pathname)
      || AGENT_SESSION_READ_PATH.test(parsed.pathname);
    if (parsed.origin !== window.location.origin || !reviewedPath) {
      throw new Error("Unreviewed Product API path");
    }
    const response = await fetch(`${parsed.pathname}${parsed.search}`, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers: requestHeaders(),
    });
    const body = await readBody(response);
    if (!response.ok) throw new RequestFailure(response, body);
    return body;
  }

  async function productMutation(path, body, options) {
    const parsed = new URL(path, window.location.origin);
    const reviewedPath = PRODUCT_MUTATION_PATHS.has(parsed.pathname)
      || DATASET_PUBLISH_MUTATION_PATH.test(parsed.pathname)
      || PRODUCT_RUN_CREATE_MUTATION_PATH.test(parsed.pathname)
      || INSTANCE_UPDATE_MUTATION_PATH.test(parsed.pathname)
      || APPROVAL_DECISION_PATH.test(parsed.pathname)
      || PRODUCT_RUN_MUTATION_PATH.test(parsed.pathname)
      || JOBS_MUTATION_PATH.test(parsed.pathname)
      || INFRA_SERVER_CONFIG_MUTATION_PATH.test(parsed.pathname)
      || INFRA_CANDIDATE_MUTATION_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_MUTATION_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_RECORD_MUTATION_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_ACTION_MUTATION_PATH.test(parsed.pathname)
      || LEGACY_DATASET_CARD_PATH.test(parsed.pathname)
      || ENGINEERING_TASK_MUTATION_PATH.test(parsed.pathname)
      || CODING_RUN_MUTATION_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_ENGINEERING_MUTATION_PATH.test(parsed.pathname)
      || LEGACY_PROJECT_AI_ENGINEER_MUTATION_PATH.test(parsed.pathname)
      || AGENT_SESSION_MUTATION_PATH.test(parsed.pathname);
    if (parsed.origin !== window.location.origin || parsed.search || !reviewedPath) {
      throw new Error("Unreviewed Product mutation path");
    }
    const headers = Object.assign(requestHeaders(), { "Content-Type": "application/json" });
    if (!options || options.idempotency !== false) {
      headers["Idempotency-Key"] = options && options.idempotencyKey
        ? options.idempotencyKey
        : randomUUID();
    }
    if (options && options.payloadDigest) {
      headers["X-Approval-Payload-Digest"] = options.payloadDigest;
    }
    //: DG-UI-UNIFICATION v1 U5: the first mutation methods beyond POST
    //: (legacy-project PATCH/DELETE, dataset-card PATCH) -- default stays
    //: "POST" so every existing call site is untouched.
    const method = (options && options.method) || "POST";
    const response = await fetch(parsed.pathname, {
      method,
      credentials: "same-origin",
      cache: "no-store",
      headers,
      body: JSON.stringify(body),
    });
    const responseBody = await readBody(response);
    if (!response.ok) throw new RequestFailure(response, responseBody);
    return responseBody;
  }

  async function authenticationRequest(path, options) {
    if (path !== "/auth/logout") throw new Error("Unreviewed authentication path");
    const response = await fetch(path, Object.assign({
      credentials: "same-origin",
      cache: "no-store",
      headers: requestHeaders(),
    }, options || {}));
    if (!response.ok) throw new RequestFailure(response, await readBody(response));
  }

  //: DG-UI-UNIFICATION v1 U6a: ported from `static/index.html`
  //: `sameOriginDownloadPath()` (:2189-2205) -- same-origin/no-credentials-
  //: in-url/no-hash/exact-normalization checks, reused for the workspace's
  //: own download surface (the sanitized collected engineering-task patch).
  function sameOriginDownloadPath(path) {
    if (typeof path !== "string" || !path.startsWith("/") || path.startsWith("//")) {
      throw new Error("下載網址不是安全的 same-origin relative path");
    }
    const parsed = new URL(path, window.location.origin);
    const normalized = `${parsed.pathname}${parsed.search}`;
    if (
      parsed.origin !== window.location.origin ||
      parsed.username ||
      parsed.password ||
      parsed.hash ||
      normalized !== path ||
      !ENGINEERING_TASK_PATCH_PATH.test(parsed.pathname)
    ) {
      throw new Error("下載網址不是安全的 same-origin relative path");
    }
    return normalized;
  }

  //: Ported from `static/index.html` `authenticatedDownload()` (:2207-2263):
  //: same Accept header preference order, same 401 short-circuit, same
  //: strict `X-Engineering-Patch-Redacted`/`X-Artifact-Semantics`/
  //: `Content-Type`/size-bound checks before the blob is trusted, retargeted
  //: at the `/api/v2/engineering-tasks/{id}/patch` wrapper.
  async function authenticatedEngineeringPatchDownload(path) {
    const safePath = sameOriginDownloadPath(path);
    const headers = Object.assign(requestHeaders(), {
      Accept: "text/x-diff, text/plain;q=0.9, application/octet-stream;q=0.5, application/json;q=0.1",
    });
    const response = await fetch(safePath, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      headers,
    });
    if (!response.ok) {
      let detail = "去敏後的已收集 patch 下載失敗";
      const contentType = (response.headers.get("Content-Type") || "").toLowerCase();
      if (contentType.startsWith("application/json")) {
        const body = await readBody(response);
        if (body && body.error && typeof body.error.message === "string") detail = body.error.message;
      }
      throw new Error(`${response.status}: ${detail}`);
    }
    const redactedHeader = (response.headers.get("X-Engineering-Patch-Redacted") || "").toLowerCase();
    if (redactedHeader !== "true" && redactedHeader !== "false") {
      throw new Error("伺服器未回傳有效的 patch 去敏狀態");
    }
    const artifactSemantics = (response.headers.get("X-Artifact-Semantics") || "").toLowerCase();
    const responseContentType = (response.headers.get("Content-Type") || "")
      .split(";", 1)[0]
      .trim()
      .toLowerCase();
    if (artifactSemantics !== "sanitized-collected-patch" || responseContentType !== "text/x-diff") {
      throw new Error("伺服器回傳的 patch artifact 語意不符");
    }
    const blob = await response.blob();
    if (blob.size < 1 || blob.size > 1024 * 1024) {
      throw new Error("伺服器回傳的 patch 大小超出可接受範圍");
    }
    return { blob, redacted: redactedHeader === "true" };
  }

  function showAlert(message) {
    const alert = element("workspace-alert");
    alert.textContent = message;
    alert.hidden = false;
  }

  function clearAlert() {
    const alert = element("workspace-alert");
    alert.textContent = "";
    alert.hidden = true;
  }

  function setAuthenticationControls() {
    const method = state.me && state.me.authentication && state.me.authentication.method;
    const actor = state.me && state.me.actor;
    const legacyOnly = state.authenticationModeKnown && !state.oidcEnabled;
    element("identity-status").textContent = actor
      ? `${actor.display_name} · ${method}`
      : "尚未登入";
    element("sign-in-btn").hidden = Boolean(actor) || !state.oidcEnabled;
    element("logout-btn").hidden = method !== "session";
    element("legacy-token-btn").hidden = !legacyOnly || method === "session";
    element("legacy-token-btn").textContent = state.legacyToken
      ? "清除暫時 legacy token"
      : "暫時使用 legacy token";
  }

  function renderRoleBadges() {
    const container = element("role-badges");
    container.replaceChildren();
    if (!state.me || !state.me.actor) return;
    if (state.me.actor.platform_admin) {
      container.append(node("span", "平台管理員", "role-badge"));
    }
    const roles = new Set();
    for (const project of state.me.project_roles || []) {
      for (const role of project.roles || []) roles.add(String(role));
    }
    for (const role of Array.from(roles).sort()) {
      container.append(node("span", ROLE_LABEL[role] || role.replaceAll("_", " "), "role-badge"));
    }
    if (!container.childElementCount) {
      container.append(node("span", "無專案角色", "role-badge"));
    }
  }

  function configureRoleAwareNavigation() {
    const roles = new Set();
    if (state.me) {
      for (const project of state.me.project_roles || []) {
        for (const role of project.roles || []) roles.add(String(role));
      }
    }
    const platformAdmin = Boolean(state.me && state.me.actor && state.me.actor.platform_admin);
    const approvalVisible = platformAdmin || roles.has("owner") || roles.has("reviewer");
    const projectReader = platformAdmin || roles.size > 0;
    const approvalNavigation = document.querySelector('[data-role-navigation="approval"]');
    const datasetNavigation = document.querySelector('[data-role-navigation="dataset"]');
    const bootstrapNavigation = document.querySelector('[data-role-navigation="bootstrap"]');
    const bootstrapCapability = state.workspace && state.workspace.capabilities && state.workspace.capabilities.project_bootstrap_v2;
    const datasetCapability = state.workspace && state.workspace.capabilities && state.workspace.capabilities.dataset_assets;
    const bootstrapVisible = platformAdmin && Boolean(bootstrapCapability && bootstrapCapability.enabled);
    approvalNavigation.hidden = !approvalVisible;
    datasetNavigation.hidden = !projectReader || !Boolean(datasetCapability && datasetCapability.enabled);
    bootstrapNavigation.hidden = !bootstrapVisible;
    element("open-bootstrap-btn").hidden = !bootstrapVisible;
    const active = document.querySelector("#workspace-navigation button.active");
    if (active && active.hidden) activateSection("overview");
  }

  function summaryCard(label, value, note) {
    const card = node("article", null, "summary-card");
    card.append(node("span", label), node("strong", String(value)), node("small", note));
    return card;
  }

  function renderSummary() {
    const workspace = state.workspace;
    const container = element("summary-cards");
    container.replaceChildren(
      summaryCard("授權範圍內的專案", workspace.projects.length, "伺服器授權範圍"),
      summaryCard(
        "近期 Run",
        workspace.recent_runs.length,
        workspace.capabilities.run_experience_v2.enabled
          ? "ExecutionPlan 投影"
          : "Legacy Job 轉接層"
      ),
      summaryCard("待核准項目", workspace.pending_approvals.length, "呼叫者可查看"),
      summaryCard("Dataset Asset", workspace.recent_dataset_assets.items.length, "授權範圍內的自有／共享")
    );
  }

  function renderCapabilities() {
    const container = element("capability-list");
    container.replaceChildren();
    for (const [key, capability] of Object.entries(state.workspace.capabilities || {})) {
      const row = node("div", null, "capability-row");
      const description = node("div");
      description.append(node("strong", key.replaceAll("_", " ")));
      description.append(node("small", capability.reason || "執行環境設定"));
      const pill = node("span", capabilityStateLabel(capability.state), "state-pill");
      if (!capability.implemented || capability.state === "unavailable") {
        pill.classList.add("unavailable");
      }
      row.append(description, pill);
      container.append(row);
    }
  }

  function appendDetail(container, label, value) {
    const wrapper = node("div");
    wrapper.append(node("dt", label), node("dd", value));
    container.append(wrapper);
  }

  //: DG-UI-UNIFICATION v1 U2 (docs/DECISIONS.md 2026-08-25): shared
  //: JSON-simplification helpers — Chinese "必要欄位摘要" replaces a raw
  //: `JSON.stringify` line while the full contract stays available in a
  //: collapsed "查看原始內容" `<details>` next to it (nothing is dropped,
  //: only re-presented).
  function resourceRequirementsSummary(resources) {
    if (!resources || typeof resources !== "object") return "-";
    const parts = [`GPU ≥${resources.min_gpu_count != null ? resources.min_gpu_count : 0}`];
    if (resources.min_gpu_memory_mb) parts.push(`GPU 記憶體 ≥${resources.min_gpu_memory_mb} MB`);
    if (resources.min_available_ram_mb) parts.push(`RAM ≥${resources.min_available_ram_mb} MB`);
    if (resources.min_available_disk_mb) parts.push(`磁碟 ≥${resources.min_available_disk_mb} MB`);
    if (Array.isArray(resources.required_tags) && resources.required_tags.length) {
      parts.push(`需要標籤 ${resources.required_tags.join(", ")}`);
    }
    parts.push(resources.exclusive_worker ? "獨占 worker" : "可共用 worker");
    return parts.join(" · ");
  }

  function keyValueLines(value) {
    if (value === null || value === undefined) return "-";
    if (Array.isArray(value)) {
      if (!value.length) return "（無）";
      return value
        .map((item) => (item && typeof item === "object")
          ? Object.entries(item).map(([key, sub]) => `${key}=${sub}`).join(", ")
          : String(item))
        .join(" ｜ ");
    }
    if (typeof value === "object") {
      const entries = Object.entries(value);
      if (!entries.length) return "（無）";
      return entries.map(([key, sub]) => `${key}: ${sub}`).join("\n");
    }
    return String(value);
  }

  function renderIdentityDetails() {
    const details = element("identity-details");
    details.replaceChildren();
    const me = state.me;
    appendDetail(details, "身分類型", me.actor.type);
    appendDetail(details, "認證方式", me.authentication.method);
    appendDetail(details, "授權模式", me.authorization.mode);
    appendDetail(details, "角色模型", me.authorization.role_model);
    appendDetail(details, "OIDC", me.authentication.oidc_enabled ? "已啟用" : "已停用");
  }

  function emptyState(title, message) {
    const wrapper = node("div", null, "empty-state");
    wrapper.append(node("strong", title), node("span", message));
    return wrapper;
  }

  function renderProjectWorkspace() {
    const panel = element("project-workspace-panel");
    const status = element("project-workspace-state");
    const details = element("project-workspace-details");
    details.replaceChildren();
    if (!state.projectWorkspace) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const workspace = state.projectWorkspace;
    status.textContent = `${workspace.project.name} · ${workspace.rbac.state}`;
    appendDetail(details, "專案 UUID", workspace.project.id);
    appendDetail(details, "RBAC 就緒狀態", workspace.rbac.state);
    appendDetail(
      details,
      "Environment",
      workspace.environment
        ? `${workspace.environment.name} · revision ${workspace.environment.revision}`
        : "尚無 Environment"
    );
    appendDetail(
      details,
      "Run Template",
      workspace.run_template
        ? `${workspace.run_template.name} · ${workspace.run_template.classification}`
        : "尚無 Run Template"
    );
    appendDetail(
      details,
      "Defaults",
      workspace.defaults ? `revision ${workspace.defaults.revision}` : "尚無 Defaults"
    );
    appendDetail(details, "資料集授權", workspace.dataset_grants.state);
  }

  async function loadProjectWorkspace(projectId) {
    const panel = element("project-workspace-panel");
    const status = element("project-workspace-state");
    panel.hidden = false;
    status.textContent = "正在載入 Project Workspace…";
    element("project-workspace-details").replaceChildren();
    try {
      state.projectWorkspace = await productRead(`/api/v2/projects/${projectId}/workspace`);
      renderProjectWorkspace();
    } catch (error) {
      state.projectWorkspace = null;
      panel.hidden = false;
      status.textContent = error instanceof Error ? error.message : "無法載入 Project Workspace";
    }
  }

  function runCreateEligibleProjects() {
    if (!state.workspace || !state.me || !state.me.actor) return [];
    const platformAdmin = Boolean(state.me.actor.platform_admin);
    return state.workspace.projects.filter((project) =>
      platformAdmin || (project.roles || []).some((role) => role === "owner" || role === "operator")
    );
  }

  function setSelectOptions(select, options, placeholder) {
    const previous = select.value;
    select.replaceChildren(new Option(placeholder, ""));
    for (const option of options) {
      select.append(new Option(option.label, option.value));
    }
    if (options.some((option) => option.value === previous)) {
      select.value = previous;
    } else if (options.length) {
      select.value = options[0].value;
    }
  }

  function runCreationCapabilityEnabled() {
    const capability = state.workspace
      && state.workspace.capabilities
      && state.workspace.capabilities.run_experience_v2;
    return Boolean(capability && capability.enabled);
  }

  function selectedRunTarget() {
    const options = state.runCreateWorkspace
      && state.runCreateWorkspace.run_creation_options
      && state.runCreateWorkspace.run_creation_options.ssh_target_candidates;
    return Array.isArray(options)
      ? options.find((option) => option.id === element("run-create-target").value) || null
      : null;
  }

  function renderRunCreateTargetOptions() {
    const select = element("run-create-target");
    const options = state.runCreateWorkspace
      && state.runCreateWorkspace.run_creation_options
      && state.runCreateWorkspace.run_creation_options.ssh_target_candidates;
    const targets = Array.isArray(options) ? options : [];
    const selectedVersion = element("run-create-project-version").value;
    setSelectOptions(
      select,
      targets.map((target) => ({
        value: target.id,
        label: `${target.server_name} · revision ${target.revision}${target.ready ? (target.instance_state === "diverged" ? " · 已就緒（exact promoted checkout）" : " · 就緒候選") : " · 需要 reconcile"}`,
      })),
      targets.length ? "選擇 SSH target" : "沒有可用的 SSH target candidate"
    );
    const matchingReady = targets.find((target) =>
      target.ready && (target.matching_promoted_version_ids || []).includes(selectedVersion)
    );
    if (matchingReady) select.value = matchingReady.id;
    const selected = selectedRunTarget();
    const note = element("run-create-target-note");
    if (!selected) {
      note.textContent = "尚無 active、approved、preflight-eligible target candidate；請先 deploy/reconcile。";
    } else if (!selected.ready) {
      note.textContent = `此 target 尚需 deploy/reconcile：${(selected.readiness_reasons || []).join(", ") || "unknown"}。Preview 仍是唯一權威。`;
    } else if (!(selected.matching_promoted_version_ids || []).includes(selectedVersion)) {
      note.textContent = "所選 ProjectVersion 尚未與此 target 的乾淨可用 instance 對齊；請選擇 matching version 或 deploy/reconcile。";
    } else {
      note.textContent = "候選具已知的乾淨可用 instance；仍必須建立 Preview 重新驗證。";
    }
    const deploy = element("run-instance-update-btn");
    deploy.disabled = !(
      selected
      && selected.update_available
      && selected.registered_instance_id
      && selectedVersion
      && !(selected.matching_promoted_version_ids || []).includes(selectedVersion)
    );
  }

  function renderRunCreateAssetOptions() {
    const select = element("run-create-asset");
    const assets = state.workspace && state.workspace.recent_dataset_assets
      && Array.isArray(state.workspace.recent_dataset_assets.items)
      ? state.workspace.recent_dataset_assets.items.filter((asset) =>
        asset.scope_project_id === state.runCreateProjectId
      )
      : [];
    setSelectOptions(
      select,
      assets.map((asset) => ({
        value: asset.asset_id,
        label: `${asset.name} · ${asset.access_mode || "owned"}`,
      })),
      assets.length ? "選擇 Dataset asset" : "沒有此 Project 可用的 Dataset asset"
    );
  }

  function renderRunCreateDatasetSelection() {
    const select = element("run-create-dataset-selection");
    const detail = state.runCreateAsset;
    const kind = element("run-create-dataset-selection-kind").value;
    const items = kind === "alias"
      ? (detail && Array.isArray(detail.active_aliases) ? detail.active_aliases : []).map((alias) => ({
        value: alias.alias_name,
        label: `${alias.alias_name} · active alias`,
      }))
      : (detail && Array.isArray(detail.snapshots) ? detail.snapshots : []).map((snapshot) => ({
        value: snapshot.snapshot_id,
        label: `${snapshot.snapshot_id} · published snapshot`,
      }));
    setSelectOptions(
      select,
      items,
      kind === "alias" ? "沒有 active alias" : "沒有 published snapshot"
    );
  }

  async function loadRunCreateAsset() {
    const assetId = element("run-create-asset").value;
    const projectId = state.runCreateProjectId;
    const requestSerial = ++state.runCreateAssetSerial;
    state.runCreateAsset = null;
    renderRunCreateForm();
    if (!assetId || !projectId) return;
    try {
      const asset = await productRead(
        `/api/v2/dataset-assets/${assetId}?project_id=${encodeURIComponent(projectId)}`
      );
      if (
        requestSerial !== state.runCreateAssetSerial
        || projectId !== state.runCreateProjectId
        || assetId !== element("run-create-asset").value
      ) return;
      state.runCreateAsset = asset;
      renderRunCreateForm();
    } catch (error) {
      if (
        requestSerial !== state.runCreateAssetSerial
        || projectId !== state.runCreateProjectId
        || assetId !== element("run-create-asset").value
      ) return;
      state.runCreateAsset = null;
      renderRunCreateForm();
      showAlert(error instanceof Error ? error.message : "無法載入 Dataset asset detail");
    }
  }

  function renderRunCreateForm() {
    const panel = element("run-create-panel");
    const enabled = runCreationCapabilityEnabled();
    const projects = runCreateEligibleProjects();
    panel.hidden = !enabled || !projects.length;
    if (!enabled || !projects.length) return;
    setSelectOptions(
      element("run-create-project"),
      projects.map((project) => ({ value: project.id, label: project.name })),
      "選擇 Project"
    );
    if (state.runCreateProjectId && projects.some((project) => project.id === state.runCreateProjectId)) {
      element("run-create-project").value = state.runCreateProjectId;
    } else {
      element("run-create-project").value = "";
    }
    const workspace = state.runCreateWorkspace;
    if (!workspace || workspace.project.id !== state.runCreateProjectId) {
      element("run-create-status").textContent = "選擇 Project 以載入安全的 Run options。";
      element("run-create-preview-btn").disabled = true;
      element("run-create-request-btn").disabled = true;
      return;
    }
    const candidates = workspace.run_creation_options || {};
    const versions = Array.isArray(candidates.project_version_candidates)
      ? candidates.project_version_candidates : [];
    setSelectOptions(
      element("run-create-project-version"),
      versions.map((version) => ({ value: version.id, label: `已核准版本 · ${formatTimestamp(version.created_at)}` })),
      versions.length ? "選擇 ProjectVersion" : "沒有 promoted ProjectVersion"
    );
    const templateOptions = [];
    if (workspace.defaults) {
      templateOptions.push({
        value: `defaults:${workspace.defaults.revision_id}`,
        label: `Project Defaults · revision ${workspace.defaults.revision}`,
      });
    }
    if (workspace.run_template) {
      templateOptions.push({
        value: `template:${workspace.run_template.id}`,
        label: `${workspace.run_template.name} · 目前 revision ${workspace.run_template.revision}`,
      });
    }
    setSelectOptions(element("run-create-template"), templateOptions, "沒有可用 Template");
    renderRunCreateTargetOptions();
    renderRunCreateAssetOptions();
    const binding = element("run-create-dataset-mode").value === "binding";
    element("run-create-binding-fields").hidden = !binding;
    if (binding) renderRunCreateDatasetSelection();
    const readyPair = selectedRunTarget()
      && selectedRunTarget().ready
      && (selectedRunTarget().matching_promoted_version_ids || []).includes(
        element("run-create-project-version").value
      );
    const bindingReady = !binding || Boolean(
      state.runCreateAsset
      && state.runCreateAsset.scope_project_id === state.runCreateProjectId
      && state.runCreateAsset.contract
      && state.runCreateAsset.contract.asset_id === element("run-create-asset").value
      && element("run-create-dataset-selection").value
      && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(
        element("run-create-binding-name").value.trim()
      )
    );
    const formReady = Boolean(
      versions.length
      && templateOptions.length
      && selectedRunTarget()
      && readyPair
      && bindingReady
    );
    element("run-create-preview-btn").disabled = !formReady;
    element("run-create-status").textContent = formReady
      ? "候選已載入；建立 Preview 前不會建立 Run 或連線到 target。"
      : "尚無 matching ready candidate；請先 deploy/reconcile，再建立 Preview。";
    renderRunCreatePreview();
  }

  async function loadRunCreateWorkspace(projectId) {
    const requestSerial = ++state.runCreateWorkspaceSerial;
    ++state.runCreateAssetSerial;
    state.runCreateProjectId = projectId || null;
    state.runCreateWorkspace = null;
    state.runCreateAsset = null;
    invalidateRunCreatePreview();
    renderRunCreateForm();
    if (!projectId) return;
    try {
      const workspace = await productRead(`/api/v2/projects/${projectId}/workspace`);
      if (
        requestSerial !== state.runCreateWorkspaceSerial
        || projectId !== state.runCreateProjectId
      ) return;
      state.runCreateWorkspace = workspace;
      renderRunCreateForm();
      await loadRunCreateAsset();
    } catch (error) {
      if (
        requestSerial !== state.runCreateWorkspaceSerial
        || projectId !== state.runCreateProjectId
      ) return;
      state.runCreateWorkspace = null;
      renderRunCreateForm();
      showAlert(error instanceof Error ? error.message : "無法載入 Run creation options");
    }
  }

  function runCreateRequestBody() {
    const workspace = state.runCreateWorkspace;
    if (!workspace || workspace.project.id !== state.runCreateProjectId) {
      throw new Error("請先選擇並載入 Project。");
    }
    let overrides;
    try {
      overrides = JSON.parse(element("run-create-overrides").value);
    } catch (_error) {
      throw new Error("Parameter overrides 必須是 JSON object。");
    }
    if (!overrides || Array.isArray(overrides) || typeof overrides !== "object") {
      throw new Error("Parameter overrides 必須是 JSON object。");
    }
    const templateValue = element("run-create-template").value;
    const [templateKind, templateId] = templateValue.split(":", 2);
    let templateSelection;
    if (templateKind === "defaults") {
      templateSelection = { kind: "project_defaults", project_defaults_revision_id: templateId };
    } else if (templateKind === "template") {
      templateSelection = { kind: "run_profile_revision", run_profile_id: templateId };
    } else {
      throw new Error("請選擇 Template。");
    }
    let datasetSelection = { kind: "none" };
    if (element("run-create-dataset-mode").value === "binding") {
      const assetId = element("run-create-asset").value;
      const selectionValue = element("run-create-dataset-selection").value;
      if (!assetId || !selectionValue) throw new Error("請選擇 Dataset asset 與 alias/snapshot。");
      const selectionKind = element("run-create-dataset-selection-kind").value;
      datasetSelection = {
        kind: "bindings",
        bindings: [{
          name: element("run-create-binding-name").value.trim(),
          asset_id: assetId,
          selection: selectionKind === "alias"
            ? { kind: "alias", alias_name: selectionValue }
            : { kind: "snapshot", snapshot_id: selectionValue },
        }],
      };
    }
    return {
      project_version_id: element("run-create-project-version").value,
      template_selection: templateSelection,
      parameter_overrides: overrides,
      dataset_selection: datasetSelection,
      target_selection: {
        kind: "server_config_revision",
        server_config_revision_id: element("run-create-target").value,
      },
    };
  }

  function renderRunCreatePreview() {
    const preview = state.runCreatePreview;
    const findings = element("run-create-findings");
    const contract = element("run-create-contract");
    findings.replaceChildren();
    element("run-create-request-btn").disabled = !preview;
    if (!preview) {
      contract.hidden = true;
      element("run-create-parameter-summary").textContent = "";
      return;
    }
    const plan = preview.plan || {};
    const commit = plan.project_version && plan.project_version.git_commit;
    element("run-create-status").textContent = "Preview ready；送出時伺服器會以 plan digest 重新驗證，僅建立 pending approval。";
    appendDetail(findings, "Plan 摘要碼", String(preview.plan_digest || "-"));
    appendDetail(
      findings,
      "專案版本",
      `${plan.project_version && plan.project_version.project_version_id || "-"} · commit ${
        typeof commit === "string" && commit ? commit.slice(0, 12) : "-"
      }`
    );
    appendDetail(findings, "範本／環境", `${plan.run_profile && plan.run_profile.run_profile_id || "-"} · ${plan.environment && plan.environment.environment_revision_id || "-"}`);
    appendDetail(findings, "資料集綁定", plan.dataset_none ? "無" : `${(plan.dataset_bindings || []).length} 個 binding`);
    appendDetail(findings, "SSH 目標", `${plan.target && plan.target.server_name || "-"} · revision ${plan.target && plan.target.server_config_revision_id || "-"}`);
    appendDetail(findings, "資源需求", resourceRequirementsSummary(plan.resource_requirements));
    appendDetail(findings, "輸出宣告摘要碼", String(plan.output_declarations_digest || "-"));
    element("run-create-parameter-summary").textContent = keyValueLines(plan.parameter_values);
    element("run-create-contract-json").textContent = JSON.stringify(plan, null, 2);
    contract.hidden = false;
  }

  function invalidateRunCreatePreview() {
    ++state.runCreatePreviewSerial;
    state.runCreatePreview = null;
    state.runCreateRequestKey = null;
    const request = element("run-create-request-btn");
    if (request) request.disabled = true;
    const contract = element("run-create-contract");
    if (contract) contract.hidden = true;
  }

  async function previewRunCreate(event) {
    event.preventDefault();
    clearAlert();
    const button = element("run-create-preview-btn");
    const previewSerial = ++state.runCreatePreviewSerial;
    const projectId = state.runCreateProjectId;
    button.disabled = true;
    element("run-create-status").textContent = "正在建立唯讀 Preview…";
    try {
      const preview = await productMutation(
        `/api/v2/projects/${projectId}/run-previews`,
        runCreateRequestBody(),
        { idempotency: false }
      );
      if (
        previewSerial !== state.runCreatePreviewSerial
        || projectId !== state.runCreateProjectId
      ) return;
      state.runCreatePreview = preview;
      state.runCreateRequestKey = randomUUID();
      renderRunCreatePreview();
    } catch (error) {
      if (
        previewSerial !== state.runCreatePreviewSerial
        || projectId !== state.runCreateProjectId
      ) return;
      state.runCreatePreview = null;
      state.runCreateRequestKey = null;
      element("run-create-request-btn").disabled = true;
      element("run-create-contract").hidden = true;
      element("run-create-parameter-summary").textContent = "";
      element("run-create-status").textContent = "Preview 未建立；請修正選項或先 deploy/reconcile。";
      showAlert(error instanceof Error ? error.message : "無法建立 Run preview");
    } finally {
      if (
        previewSerial !== state.runCreatePreviewSerial
        || projectId !== state.runCreateProjectId
      ) return;
      button.disabled = false;
    }
  }

  async function requestRunCreate() {
    if (!state.runCreatePreview || !state.runCreateProjectId) return;
    const button = element("run-create-request-btn");
    button.disabled = true;
    clearAlert();
    element("run-create-status").textContent = "正在建立 immutable pending approval…";
    try {
      const result = await productMutation(
        `/api/v2/projects/${state.runCreateProjectId}/run-requests`,
        { ...runCreateRequestBody(), expected_plan_digest: state.runCreatePreview.plan_digest },
        { idempotencyKey: state.runCreateRequestKey }
      );
      await initialize();
      activateSection("approvals");
      showAlert(`Run approval #${result.approval_id} 已建立，尚未直接執行。`);
    } catch (error) {
      button.disabled = false;
      element("run-create-status").textContent = "Request 未完成；未變更欄位時可使用同一 idempotency identity 安全重試。";
      showAlert(error instanceof Error ? error.message : "無法建立 Run approval");
    }
  }

  async function requestSelectedInstanceUpdate() {
    const target = selectedRunTarget();
    const projectId = state.runCreateProjectId;
    const projectVersionId = element("run-create-project-version").value;
    if (!target || !target.registered_instance_id || !projectId || !projectVersionId) return;
    const button = element("run-instance-update-btn");
    button.disabled = true;
    clearAlert();
    element("run-create-status").textContent = "正在建立既有 instance 的唯讀 deployment Preview…";
    try {
      const selection = { project_version_id: projectVersionId, instance_id: target.registered_instance_id };
      const preview = await productMutation(
        `/api/v2/projects/${projectId}/instance-update-previews`, selection, { idempotency: false }
      );
      const result = await productMutation(
        `/api/v2/projects/${projectId}/instance-update-requests`,
        { ...selection, expected_preview_digest: preview.preview_digest },
        { idempotencyKey: randomUUID() }
      );
      await initialize();
      activateSection("approvals");
      showAlert(`所選版本部署核准 #${result.approval_id} 已建立；核准前不會變更 checkout。`);
    } catch (error) {
      element("run-create-status").textContent = "Deployment Preview/Request 未完成；target 可能需要 reconcile。";
      showAlert(error instanceof Error ? error.message : "無法建立 instance deployment approval");
    } finally {
      renderRunCreateForm();
    }
  }

  function renderProjects() {
    const container = element("project-list");
    container.replaceChildren();
    if (!state.workspace.projects.length) {
      container.append(emptyState("沒有可見 Project", "伺服器沒有回傳 caller 可查看的 Project。"));
      renderProjectWorkspace();
      renderLegacyProjectsMatrix();
      return;
    }
    for (const project of state.workspace.projects) {
      const card = node("article", null, "item-card");
      const header = node("div", null, "item-card-header");
      const roleLabels = (project.roles || []).map((role) => ROLE_LABEL[role] || role).join(" · ");
      header.append(node("h3", project.name), node("span", roleLabels, "honesty-label"));
      card.append(header, node("p", project.summary || "尚未提供摘要。"));
      const meta = node("div", null, "item-meta");
      meta.append(node("span", `專案 ID · ${project.id}`), node("span", formatTimestamp(project.created_at)));
      card.append(meta);
      //: DG-UI-UNIFICATION v1 U5: legacy summary info merged onto the
      //: existing Product v2 project card -- v2 typed projection stays
      //: authoritative for *which* projects show a card; legacy-only facts
      //: (instance health, hub head, active jobs, last run) are appended
      //: when available (`state.legacyProjectsByName`, loaded separately by
      //: `loadLegacyProjectsSummary()` -- not part of `/api/v2/workspace`).
      const legacy = state.legacyProjectsByName[project.name];
      if (legacy) {
        const legacyMeta = node("div", null, "item-meta");
        const counts = legacy.instanceCounts;
        const instanceLine = legacy.instanceTotal
          ? `instance ${legacy.instanceTotal} 台（可用 ${counts.available || 0}／缺 ${counts.missing || 0}／`
            + `有未提交 ${counts.dirty || 0}／分歧 ${counts.diverged || 0}／未知 ${counts.unknown || 0}）`
          : "尚無已登記 instance";
        legacyMeta.append(node("span", instanceLine));
        if (legacy.hub && legacy.hub.exists && legacy.hub.head) {
          legacyMeta.append(node("span", `最新 commit ${String(legacy.hub.head).slice(0, 12)}`));
        }
        if (legacy.datasetName) {
          legacyMeta.append(node("span", `資料集 ${legacy.datasetName}@${legacy.datasetVersion}`));
        }
        legacyMeta.append(node(
          "span",
          `活躍 Jobs ${legacy.activeJobs}（AI 任務 ${legacy.activeAiJobs}）`
        ));
        legacyMeta.append(node(
          "span",
          legacy.lastRun
            ? `最近執行 #${legacy.lastRun.id} · ${formatTimestamp(legacy.lastRun.created_at)}`
            : "尚無執行紀錄"
        ));
        card.append(legacyMeta);
      }
      const actions = node("div", null, "button-row");
      const open = node("button", "查看 Workspace", "button button-quiet");
      open.type = "button";
      open.addEventListener("click", () => loadProjectWorkspace(project.id));
      actions.append(open);
      const detail = node("button", "詳情", "button button-quiet");
      detail.type = "button";
      detail.addEventListener("click", () => openLegacyProjectDetail(project.name));
      actions.append(detail);
      const dispatch = node("button", "派工", "button button-quiet");
      dispatch.type = "button";
      dispatch.addEventListener("click", () => jumpToJobDispatch(project.name));
      actions.append(dispatch);
      const del = node("button", "刪除", "button button-quiet");
      del.type = "button";
      del.addEventListener("click", () => deleteLegacyProjectAction(project.name));
      actions.append(del);
      card.append(actions);
      container.append(card);
    }
    renderProjectWorkspace();
    renderLegacyProjectsMatrix();
  }

  // ---------------------------------------------------------------------
  // 專案（legacy 相容，DG-UI-UNIFICATION v1 U5）：專案列表卡片合併 legacy
  // 摘要、新增專案、專案×伺服器矩陣、專案詳情面板（總覽／程式版本／資料與
  // 產出／設定／部署／時間軸／活動探測）。全部走 `/api/v2/legacy-projects*`
  // thin wrapper（`dispatch_center/api/routers/projects_legacy_v2.py`）。
  // ---------------------------------------------------------------------

  async function loadLegacyProjectsSummary() {
    try {
      const [matrix, jobs] = await Promise.all([
        productRead("/api/v2/projects-matrix"),
        productRead("/api/v2/jobs"),
      ]);
      state.legacyProjectsMatrix = matrix;
      const byName = {};
      for (const project of matrix.projects || []) {
        const instanceStates = Object.values(project.instances || {}).map(
          (instance) => instance.state || "unknown"
        );
        const counts = { available: 0, missing: 0, dirty: 0, diverged: 0, unknown: 0 };
        for (const value of instanceStates) counts[value] = (counts[value] || 0) + 1;
        byName[project.name] = {
          repoOrPath: project.repo_or_path,
          instanceCounts: counts,
          instanceTotal: instanceStates.length,
          hub: project.hub,
          datasetName: null,
          datasetVersion: null,
          activeJobs: 0,
          activeAiJobs: 0,
          lastRun: null,
        };
      }
      for (const job of Array.isArray(jobs) ? jobs : []) {
        const entry = job.project ? byName[job.project] : null;
        if (!entry) continue;
        if (job.status === "running" || job.status === "queued") {
          entry.activeJobs += 1;
          //: 近似值：legacy scope 沒有獨立的「AI 任務」集合，這裡用
          //: `type === "coding"`（AI 改碼／Codex 任務固定走這個 job type）
          //: 近似「活躍 AI 任務數」，UX 簡化，已記錄在 U5 完成報告。
          if (job.type === "coding") entry.activeAiJobs += 1;
        }
        if (!entry.lastRun || (job.created_at || "") > (entry.lastRun.created_at || "")) {
          entry.lastRun = job;
        }
      }
      //: dataset binding comes from the legacy project list, not the
      //: Product v2 projection (which doesn't carry `dataset_name`).
      const legacyProjects = await productRead("/api/v2/legacy-projects");
      for (const project of Array.isArray(legacyProjects) ? legacyProjects : []) {
        const entry = byName[project.name];
        if (entry && project.dataset_name) {
          entry.datasetName = project.dataset_name;
          entry.datasetVersion = project.dataset_version;
        }
      }
      state.legacyProjectsByName = byName;
      state.legacyProjectsSummaryLoaded = true;
    } catch (error) {
      state.legacyProjectsByName = {};
      state.legacyProjectsMatrix = null;
      state.legacyProjectsSummaryLoaded = false;
    }
    renderProjects();
  }

  function renderLegacyProjectsMatrix() {
    const tbody = element("legacy-matrix-tbody");
    tbody.replaceChildren();
    const stateEl = element("legacy-matrix-state");
    const matrix = state.legacyProjectsMatrix;
    if (!matrix) {
      stateEl.textContent = "尚未載入矩陣。";
      element("legacy-matrix-pending-candidates").replaceChildren();
      return;
    }
    stateEl.textContent = `共 ${matrix.projects.length} 個專案 · ${matrix.servers.length} 台伺服器。`;
    for (const project of matrix.projects) {
      const servers = Object.keys(project.instances || {});
      if (!servers.length) {
        const row = node("tr");
        row.append(
          node("td", project.name),
          node("td", "（尚無已登記機器）"),
          node("td", "-"),
          node("td", project.hub && project.hub.head ? String(project.hub.head).slice(0, 12) : "-")
        );
        const actionCell = node("td");
        const deployBtn = node("button", "部署", "button button-quiet");
        deployBtn.type = "button";
        deployBtn.addEventListener("click", () => openLegacyProjectDetail(project.name, "deploy"));
        actionCell.append(deployBtn);
        row.append(actionCell);
        tbody.append(row);
        continue;
      }
      for (const server of servers) {
        const instance = project.instances[server];
        const row = node("tr");
        row.append(
          node("td", project.name),
          node("td", server),
          node("td", window.WorkspaceUI.instanceStateLabel(instance.state)),
          node("td", project.hub && project.hub.head ? String(project.hub.head).slice(0, 12) : "-")
        );
        const actionCell = node("div", null, "button-row");
        const gitInitBtn = node("button", "git 化", "button button-quiet");
        gitInitBtn.type = "button";
        gitInitBtn.addEventListener("click", () => gitInitAction(project.name, server));
        const hubSyncBtn = node("button", "hub 同步", "button button-quiet");
        hubSyncBtn.type = "button";
        hubSyncBtn.addEventListener("click", () => hubSyncAction(project.name, server));
        actionCell.append(gitInitBtn, hubSyncBtn);
        const actionTd = node("td");
        actionTd.append(actionCell);
        row.append(actionTd);
        tbody.append(row);
      }
    }
    const pendingContainer = element("legacy-matrix-pending-candidates");
    pendingContainer.replaceChildren();
    const withPending = Object.entries(matrix.pending_candidates || {}).filter(
      ([, count]) => count > 0
    );
    if (withPending.length) {
      const line = withPending.map(([server, count]) => `${server}：${count}`).join("、");
      const goInfra = node("button", `尚有候選未處理（${line}）→ 前往基礎設施`, "button button-quiet");
      goInfra.type = "button";
      goInfra.addEventListener("click", () => activateSection("infrastructure"));
      pendingContainer.append(goInfra);
    }
  }

  async function gitInitAction(projectName, server) {
    try {
      const approval = await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/git-init-requests`,
        { server }
      );
      showAlert(`git 化核准 #${approval.id} 已建立，待核准後才會就地初始化。`);
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "git 化請求失敗");
    }
  }

  async function hubSyncAction(projectName, server) {
    try {
      const result = await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/hub-sync`,
        { server }
      );
      showAlert(`已同步至中央 hub：${result.head ? String(result.head).slice(0, 12) : "unknown"}`);
      await loadLegacyProjectsSummary();
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "hub 同步失敗");
    }
  }

  function jumpToJobDispatch(projectName) {
    activateSection("jobs");
    const select = element("job-dispatch-project");
    if (Array.from(select.options).some((option) => option.value === projectName)) {
      select.value = projectName;
    }
    element("job-dispatch-command").focus();
  }

  async function deleteLegacyProjectAction(projectName) {
    if (!window.confirm(`確定要刪除專案 ${projectName}？只會刪除 DB 登記，不會動任何機器上的檔案。`)) {
      return;
    }
    try {
      await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}`,
        {},
        { method: "DELETE", idempotency: false }
      );
      showAlert(`專案 ${projectName} 已刪除。`);
      if (state.legacyProjectDetailName === projectName) closeLegacyProjectDetail();
      await Promise.all([loadWorkspace(), loadLegacyProjectsSummary()]);
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "刪除專案失敗");
    }
  }

  function renderLegacyProjectCreateForm(visible) {
    element("legacy-project-create-form").hidden = !visible;
    element("legacy-project-create-toggle-btn").textContent = visible ? "取消新增專案" : "新增專案";
  }

  async function submitLegacyProjectCreate() {
    const name = element("legacy-project-create-name").value.trim();
    const repo = element("legacy-project-create-repo").value.trim();
    const datasetName = element("legacy-project-create-dataset-name").value.trim();
    const datasetVersion = element("legacy-project-create-dataset-version").value.trim();
    const defaultCommand = element("legacy-project-create-default-command").value.trim();
    const requireTag = element("legacy-project-create-require-tag").value.trim();
    const setupCmd = element("legacy-project-create-setup-cmd").value.trim();
    const status = element("legacy-project-create-status");
    if (!name || !repo) {
      status.textContent = "名稱與 repo_or_path 為必填。";
      return;
    }
    try {
      await productMutation("/api/v2/legacy-projects", {
        name,
        repo_or_path: repo,
        dataset_name: datasetName || null,
        dataset_version: datasetVersion || null,
        default_command: defaultCommand || null,
        require_tag: requireTag || null,
        setup_cmd: setupCmd || null,
      });
      status.textContent = `專案 ${name} 已建立。`;
      renderLegacyProjectCreateForm(false);
      await Promise.all([loadWorkspace(), loadLegacyProjectsSummary()]);
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "建立專案失敗";
    }
  }

  function legacyProjectDetailTabs() {
    return Array.from(document.querySelectorAll("[data-legacy-detail-tab]"));
  }

  function activateLegacyProjectDetailTab(tab) {
    state.legacyProjectDetailTab = tab;
    for (const button of legacyProjectDetailTabs()) {
      button.classList.toggle("active", button.getAttribute("data-legacy-detail-tab") === tab);
    }
    for (const panel of document.querySelectorAll("[data-legacy-detail-panel]")) {
      panel.hidden = panel.getAttribute("data-legacy-detail-panel") !== tab;
    }
    if (tab === "timeline" && !state.legacyProjectTimeline) loadLegacyProjectTimeline(true);
  }

  function closeLegacyProjectDetail() {
    state.legacyProjectDetailName = null;
    state.legacyProjectDetail = null;
    state.legacyProjectTimeline = null;
    state.legacyProjectActivity = null;
    element("legacy-project-detail-panel").hidden = true;
    //: DG-UI-UNIFICATION v1 U6b: both AI Engineer sub-panels reset their own
    //: load/poll lifecycle independently, mirroring legacy
    //: `closeProjectDetail()`/`openProjectDetail()`'s reset-then-load pairing.
    resetAgentSessionPanel();
    resetAIConversationPanel();
  }

  async function openLegacyProjectDetail(projectName, tab) {
    state.legacyProjectDetailName = projectName;
    state.legacyProjectTimeline = null;
    state.legacyProjectActivity = null;
    element("legacy-project-detail-panel").hidden = false;
    element("legacy-project-detail-title").textContent = `專案詳情 · ${projectName}`;
    activateLegacyProjectDetailTab(tab || "overview");
    resetAgentSessionPanel();
    resetAIConversationPanel();
    loadAgentSessionPanel(projectName);
    loadAIConversationPanel(projectName);
    try {
      const [detail, versions] = await Promise.all([
        productRead(`/api/v2/legacy-projects/${encodeURIComponent(projectName)}/detail`),
        productRead(`/api/v2/legacy-projects/${encodeURIComponent(projectName)}/versions`),
      ]);
      state.legacyProjectDetail = Object.assign({}, detail, { versions });
      renderLegacyProjectDetail();
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "無法載入專案詳情");
    }
  }

  function renderLegacyProjectDetail() {
    const detail = state.legacyProjectDetail;
    if (!detail) return;
    const project = detail.project;

    const metrics = element("legacy-project-detail-metrics");
    metrics.replaceChildren(
      summaryCard("Instance", detail.instances.length, "已登記機器"),
      summaryCard("ProjectVersion", detail.versions.length, "版本歷史"),
      summaryCard("Hub", detail.hub && detail.hub.exists ? "已同步" : "尚未同步", "中央 hub"),
    );
    //: U5 baseline 刻意用 <pre>/textContent（不是 legacy 的 escape-first
    //: `renderMarkdown`）——純文字呈現，不解析任何 Markdown 語法，見完成
    //: 報告「UX 簡化」一節。
    element("legacy-project-detail-goal").textContent = project.goal || "（尚未填寫）";
    element("legacy-project-detail-progress").textContent = project.progress || "（尚未填寫）";

    const instanceList = element("legacy-project-detail-instances");
    instanceList.replaceChildren();
    for (const instance of detail.instances) {
      const card = node("article", null, "item-card");
      card.append(node("h3", instance.server));
      card.append(node("p", `${instance.path} · ${window.WorkspaceUI.instanceStateLabel(instance.state)}`));
      if (instance.git_commit) {
        card.append(node("small", `${instance.git_branch || "-"}@${String(instance.git_commit).slice(0, 12)}`));
      }
      instanceList.append(card);
    }

    const versionsBody = element("legacy-project-detail-versions-tbody");
    versionsBody.replaceChildren();
    for (const version of detail.versions) {
      const row = node("tr");
      row.append(
        node("td", formatTimestamp(version.created_at)),
        node("td", `${version.git_ref || "-"}@${String(version.git_commit || "").slice(0, 12)}`),
        node("td", version.source_instance_id ? `instance #${version.source_instance_id}` : "-")
      );
      versionsBody.append(row);
    }

    const datasetDl = element("legacy-project-detail-dataset");
    datasetDl.replaceChildren();
    appendDetail(
      datasetDl,
      "資料集綁定",
      project.dataset_name ? `${project.dataset_name}@${project.dataset_version}` : "（未綁定）"
    );
    appendDetail(datasetDl, "dataset_mode", project.dataset_mode || "（未設定）");

    const settingsDl = element("legacy-project-detail-settings");
    settingsDl.replaceChildren();
    appendDetail(settingsDl, "default_command", project.default_command || "（未設定）");
    appendDetail(settingsDl, "setup_cmd", project.setup_cmd || "（未設定）");
    appendDetail(settingsDl, "require_tag", project.require_tag || "（未設定）");
    element("legacy-project-detail-goal-input").value = project.goal || "";
    element("legacy-project-detail-notes-input").value = project.optimization_notes || "";
    element("legacy-project-detail-progress-input").value = project.progress || "";

    const deployDl = element("legacy-project-detail-deploy");
    deployDl.replaceChildren();
    appendDetail(deployDl, "Hub head", detail.hub && detail.hub.head ? String(detail.hub.head).slice(0, 12) : "（尚未同步）");
    appendDetail(
      deployDl,
      "最新版本",
      detail.versions.length
        ? `${detail.versions[0].git_ref || "-"}@${String(detail.versions[0].git_commit || "").slice(0, 12)}`
        : "（尚無版本紀錄）"
    );

    renderLegacyProjectTimelineKindFilters();
  }

  async function submitLegacyProjectDocs() {
    const projectName = state.legacyProjectDetailName;
    if (!projectName) return;
    const status = element("legacy-project-detail-docs-status");
    try {
      await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}`,
        {
          goal: element("legacy-project-detail-goal-input").value,
          optimization_notes: element("legacy-project-detail-notes-input").value,
          progress: element("legacy-project-detail-progress-input").value,
        },
        { method: "PATCH" }
      );
      status.textContent = "已儲存文件卡。";
      await Promise.all([openLegacyProjectDetail(projectName, "settings"), loadLegacyProjectsSummary()]);
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "儲存失敗";
    }
  }

  async function submitLegacyProjectDeploy() {
    const projectName = state.legacyProjectDetailName;
    if (!projectName) return;
    const target = element("legacy-project-deploy-target").value.trim();
    const status = element("legacy-project-deploy-status");
    if (!target) {
      status.textContent = "目標伺服器為必填。";
      return;
    }
    try {
      const approval = await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/deploy-requests`,
        { target_server: target }
      );
      status.textContent = `部署核准 #${approval.id} 已建立，待核准後才會實際部署。`;
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "部署請求失敗";
    }
  }

  const LEGACY_TIMELINE_KINDS = ["record", "job", "coding_run"];

  function renderLegacyProjectTimelineKindFilters() {
    const container = element("legacy-project-timeline-kinds");
    container.replaceChildren();
    for (const kind of LEGACY_TIMELINE_KINDS) {
      const label = node("label", null, "field field-inline");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.legacyProjectTimelineKinds.has(kind);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) state.legacyProjectTimelineKinds.add(kind);
        else state.legacyProjectTimelineKinds.delete(kind);
        loadLegacyProjectTimeline(true);
      });
      label.append(checkbox, node("span", kind));
      container.append(label);
    }
  }

  async function loadLegacyProjectTimeline(reset) {
    const projectName = state.legacyProjectDetailName;
    if (!projectName) return;
    const list = element("legacy-project-timeline-list");
    if (reset) {
      state.legacyProjectTimeline = null;
      list.replaceChildren(emptyState("正在載入時間軸…", "合併 record／job／coding_run 三源。"));
    }
    const params = new URLSearchParams({ limit: "20" });
    const q = element("legacy-project-timeline-q").value.trim();
    if (q) params.set("q", q);
    if (state.legacyProjectTimelineKinds.size) {
      params.set("kinds", Array.from(state.legacyProjectTimelineKinds).join(","));
    }
    if (!reset && state.legacyProjectTimeline && state.legacyProjectTimeline.next_before_ts) {
      params.set("before_ts", state.legacyProjectTimeline.next_before_ts);
    }
    try {
      const page = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/timeline?${params.toString()}`
      );
      if (reset || !state.legacyProjectTimeline) {
        state.legacyProjectTimeline = page;
      } else {
        state.legacyProjectTimeline = {
          items: state.legacyProjectTimeline.items.concat(page.items),
          next_before_ts: page.next_before_ts,
          has_more: page.has_more,
        };
      }
      renderLegacyProjectTimeline();
    } catch (error) {
      list.replaceChildren(
        emptyState("無法載入時間軸", error instanceof Error ? error.message : "未知錯誤")
      );
    }
  }

  function renderLegacyProjectTimeline() {
    const list = element("legacy-project-timeline-list");
    list.replaceChildren();
    const page = state.legacyProjectTimeline;
    const moreBtn = element("legacy-project-timeline-more-btn");
    if (!page || !page.items.length) {
      list.append(emptyState("沒有符合條件的時間軸事件", "調整搜尋或類型篩選再試一次。"));
      moreBtn.hidden = true;
      return;
    }
    for (const item of page.items) {
      const card = node("article", null, "timeline-item");
      card.append(node("strong", `${item.type}／${item.kind}`), node("span", formatTimestamp(item.timestamp)));
      if (item.title) card.append(node("p", item.title));
      if (item.content) card.append(node("p", item.content));
      if (item.type === "job" || item.type === "coding_run") {
        const jump = node("button", "查看日誌（前往工作區）", "button button-quiet");
        jump.type = "button";
        jump.addEventListener("click", () => {
          activateSection("jobs");
        });
        card.append(jump);
      }
      list.append(card);
    }
    moreBtn.hidden = !page.has_more;
  }

  async function submitLegacyProjectRecord() {
    const projectName = state.legacyProjectDetailName;
    if (!projectName) return;
    const content = element("legacy-project-record-content").value.trim();
    const status = element("legacy-project-record-status");
    if (!content) {
      status.textContent = "內容為必填。";
      return;
    }
    try {
      await productMutation(`/api/v2/legacy-projects/${encodeURIComponent(projectName)}/records`, {
        content,
        kind: element("legacy-project-record-kind").value,
        title: element("legacy-project-record-title").value.trim() || null,
      });
      status.textContent = "已新增紀錄。";
      element("legacy-project-record-content").value = "";
      element("legacy-project-record-title").value = "";
      await loadLegacyProjectTimeline(true);
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "新增紀錄失敗";
    }
  }

  async function probeLegacyProjectActivity() {
    const projectName = state.legacyProjectDetailName;
    if (!projectName) return;
    const status = element("legacy-project-activity-state");
    status.textContent = "正在探測執行近況（會對在線機器直接發起唯讀 SSH）…";
    try {
      const activity = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/activity`
      );
      state.legacyProjectActivity = activity;
      renderLegacyProjectActivity();
      status.textContent = "探測完成。";
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "探測失敗";
    }
  }

  function renderLegacyProjectActivity() {
    const container = element("legacy-project-activity-result");
    container.replaceChildren();
    const activity = state.legacyProjectActivity;
    if (!activity) return;
    if (typeof activity.activity === "string") {
      container.append(emptyState("尚無可探測的機器", activity.activity));
      return;
    }
    const jobsCard = node("article", null, "item-card");
    jobsCard.append(node("h3", "近期任務"));
    for (const job of activity.recent_jobs || []) {
      jobsCard.append(node("p", `#${job.id} · ${job.status} · ${job.server || "-"}`));
    }
    container.append(jobsCard);
    for (const probe of activity.activity) {
      const card = node("article", null, "item-card");
      card.append(node("h3", probe.server));
      if (probe.skipped) {
        card.append(node("p", `已略過（${probe.skipped}）`));
      } else {
        card.append(node("p", probe.path));
        const details = node("details");
        details.append(node("summary", "近期變動檔案與 log 尾段"));
        const pre = node("pre", JSON.stringify(probe, null, 2), "approval-summary-pre");
        details.append(pre);
        card.append(details);
      }
      container.append(card);
    }
  }

  // ---------------------------------------------------------------------
  // AI Engineer（DG-UI-UNIFICATION v1 U6b）：AgentSession Development
  // Session workbench（DG-AGENT-SESSION-V1 P4）＋每 project 一個 AI
  // conversation（DG-CONVERSATION-V1 CV-2a），ported from `static/ui.js`
  // (:3406-4570)/`static/index.html` (:565-689) into the U5 project detail
  // tab bar's new "ai-engineer" tab. Both flag-gated sub-sections use the
  // same 404-probe-hides pattern as legacy (CV-6): a 404 from the first
  // load hides the whole `<section>`, not an error card.
  // ---------------------------------------------------------------------

  function agentSessionShowPanel(name) {
    ["legacy-agent-session-open-panel", "legacy-agent-session-pending-panel", "legacy-agent-session-workbench"].forEach((id) => {
      const el = element(id);
      if (el) el.hidden = id !== name;
    });
  }

  function agentSessionSetComposerEnabled(enabled) {
    const input = element("legacy-agent-session-input");
    const sendBtn = element("legacy-agent-session-send-btn");
    if (input) input.disabled = !enabled;
    if (sendBtn) sendBtn.disabled = !enabled;
  }

  //: Single always-current "目前狀態" line (product plan §17). Every call
  //: replaces the previous text -- this is a status, not a log; the
  //: friendly event feed and the collapsed 技術細節 block are the append-
  //: only history.
  function agentSessionSetLiveStatus(text) {
    const el = element("legacy-agent-session-live-status-text");
    if (el) el.textContent = text;
  }

  function agentSessionStopPolling() {
    // Bumping the serial invalidates any already-scheduled `setTimeout`
    // callback even if it fires after this call (single poll loop, ever).
    state.agentSessionPollSerial += 1;
    if (state.agentSessionPollTimer) {
      clearTimeout(state.agentSessionPollTimer);
      state.agentSessionPollTimer = null;
    }
  }

  function agentSessionCheckpointStopPolling() {
    state.agentSessionCheckpointPollSerial += 1;
    if (state.agentSessionCheckpointPollTimer) {
      clearTimeout(state.agentSessionCheckpointPollTimer);
      state.agentSessionCheckpointPollTimer = null;
    }
  }

  function agentSessionPromoteStopPolling() {
    state.agentSessionPromotePollSerial += 1;
    if (state.agentSessionPromotePollTimer) {
      clearTimeout(state.agentSessionPromotePollTimer);
      state.agentSessionPromotePollTimer = null;
    }
  }

  function resetAgentSessionPanel() {
    state.agentSessionLoadSerial += 1;
    state.agentSessionProjectName = null;
    state.agentSessionCurrent = null;
    state.agentSessionPendingApprovalId = null;
    state.agentSessionReady = false;
    state.agentSessionSubmittingOpen = false;
    state.agentSessionSubmittingMessage = false;
    state.agentSessionClosing = false;
    agentSessionStopPolling();
    agentSessionCheckpointStopPolling();
    agentSessionPromoteStopPolling();
    state.agentSessionCheckpointApprovalId = null;
    state.agentSessionCheckpointStatus = null;
    state.agentSessionCheckpointBridgeTaskId = null;
    state.agentSessionCheckpointBusy = false;
    state.agentSessionPromoteApprovalId = null;
    state.agentSessionPromoteStatus = null;
    state.agentSessionPromoteBusy = false;
    const section = element("legacy-agent-session-section");
    if (section) section.hidden = true;
    agentSessionShowPanel(null);
    const openForm = element("legacy-agent-session-open-form");
    if (openForm) openForm.reset();
    const messageForm = element("legacy-agent-session-form");
    if (messageForm) messageForm.reset();
    const versionSelect = element("legacy-agent-session-version");
    if (versionSelect) versionSelect.replaceChildren();
    const messages = element("legacy-agent-session-messages");
    if (messages) messages.replaceChildren();
    const events = element("legacy-agent-session-events");
    if (events) events.replaceChildren();
    const turnPanel = element("legacy-agent-session-turn-panel");
    if (turnPanel) turnPanel.hidden = true;
    const turnStatus = element("legacy-agent-session-turn-status");
    if (turnStatus) turnStatus.textContent = "";
    state.agentSessionTurnRawEvents = [];
    //: 技術細節 <details> stays without an `open` attribute -- collapsed by
    //: default (product plan §17), so `.open = false` here only clears a
    //: user's manual expand from a previous session, never forces it open.
    const techDetails = element("legacy-agent-session-turn-tech-details");
    if (techDetails) techDetails.open = false;
    const techJson = element("legacy-agent-session-turn-tech-json");
    if (techJson) techJson.textContent = "";
    agentSessionSetLiveStatus("尚未開始，請在下方輸入訊息。");
    const diffBox = element("legacy-agent-session-diff");
    if (diffBox) { diffBox.hidden = true; diffBox.replaceChildren(); }
    const statusBox = element("legacy-agent-session-status");
    if (statusBox) statusBox.textContent = "";
    const checkpointStatusBox = element("legacy-agent-session-checkpoint-status");
    if (checkpointStatusBox) checkpointStatusBox.textContent = "";
    const checkpointBtn = element("legacy-agent-session-checkpoint-btn");
    if (checkpointBtn) checkpointBtn.disabled = true;
    const promoteBtn = element("legacy-agent-session-promote-btn");
    if (promoteBtn) { promoteBtn.hidden = true; promoteBtn.disabled = false; }
    const pendingText = element("legacy-agent-session-pending-text");
    if (pendingText) pendingText.textContent = "";
    agentSessionSetComposerEnabled(false);
  }

  function agentSessionMakeMessageItem(message) {
    const item = node("div", null, `pd-ai-message pd-ai-message-${message.role === "user" ? "user" : "assistant"}`);
    item.append(node("strong", message.role === "user" ? "你" : "AI"), node("p", message.content));
    return item;
  }

  function agentSessionRenderMessagesList(messages) {
    const list = element("legacy-agent-session-messages");
    if (!list) return;
    list.replaceChildren();
    if (!messages.length) {
      list.append(emptyState("尚無訊息", "輸入一句話開始這個工作階段。"));
      return;
    }
    messages.forEach((message) => list.append(agentSessionMakeMessageItem(message)));
    list.scrollTop = list.scrollHeight;
  }

  function agentSessionAppendMessage(message) {
    const list = element("legacy-agent-session-messages");
    if (!list || !message) return;
    list.append(agentSessionMakeMessageItem(message));
    list.scrollTop = list.scrollHeight;
  }

  async function agentSessionLoadMessages(projectName, serial) {
    try {
      const data = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/conversation`
      );
      if (serial !== state.agentSessionLoadSerial || state.agentSessionProjectName !== projectName) return;
      agentSessionRenderMessagesList((data && data.messages) || []);
    } catch (error) {
      // Best-effort only -- the AI conversation pane below remains the
      // source of truth for this shared conversation; a failed fetch just
      // leaves this workbench's own list empty rather than blocking it.
    }
  }

  function agentSessionRenderPendingGuidance() {
    const text = element("legacy-agent-session-pending-text");
    if (!text) return;
    const approvalId = state.agentSessionPendingApprovalId;
    text.textContent = approvalId != null
      ? `已建立核准卡（agent_session_open），請至「核准」區決定（#${approvalId}）。核准後回來按「重新整理」查看最新狀態。`
      : "已建立核准卡（agent_session_open），請至「核准」區決定，核准後回來按「重新整理」查看最新狀態。";
  }

  function agentSessionRenderWorkbench(session) {
    const turnsEl = element("legacy-agent-session-turns");
    if (turnsEl) {
      turnsEl.textContent = `已進行 ${session.turn_count}/${session.max_turns} 回合` +
        (session.active_turn_no != null ? `（第 ${session.active_turn_no} 回合進行中）` : "");
    }
    const limitReached = session.turn_count >= session.max_turns;
    const busy = state.agentSessionSubmittingMessage || session.active_turn_no != null;
    agentSessionSetComposerEnabled(!limitReached && !busy);
    const statusBox = element("legacy-agent-session-status");
    if (statusBox && limitReached) {
      statusBox.textContent = "已達本次工作階段對話上限（200 回合），請結束後開新的工作階段。";
    }
    if (session.active_turn_no != null) {
      agentSessionStartTurnPolling(session.id, session.active_turn_no);
    } else if (limitReached) {
      agentSessionSetLiveStatus("已達對話上限，請結束後開新的工作階段。");
    } else {
      agentSessionSetLiveStatus(
        session.turn_count > 0 ? "回合完成，等你回覆。" : "尚未開始，請在下方輸入訊息。"
      );
    }
    agentSessionUpdateCheckpointButtons();
  }

  function agentSessionUpdateCheckpointButtons() {
    const session = state.agentSessionCurrent;
    const checkpointBtn = element("legacy-agent-session-checkpoint-btn");
    const promoteBtn = element("legacy-agent-session-promote-btn");
    if (!checkpointBtn || !promoteBtn) return;
    const sessionReady = !!session && session.status === "active" && session.active_turn_no == null;
    const checkpointInFlight = state.agentSessionCheckpointStatus === "pending" || state.agentSessionCheckpointBusy;
    checkpointBtn.disabled = !sessionReady || checkpointInFlight;
    const canPromote = state.agentSessionCheckpointStatus === "approved"
      && !!state.agentSessionCheckpointBridgeTaskId
      && state.agentSessionPromoteStatus !== "approved";
    promoteBtn.hidden = !canPromote;
    promoteBtn.disabled = state.agentSessionPromoteBusy || state.agentSessionPromoteStatus === "pending";
  }

  async function agentSessionLoadOpenForm(projectName, serial) {
    const versionSelect = element("legacy-agent-session-version");
    const openBtn = element("legacy-agent-session-open-btn");
    const help = element("legacy-agent-session-version-help");
    if (!versionSelect) return;
    versionSelect.replaceChildren(new Option("載入中…", ""));
    versionSelect.disabled = true;
    if (openBtn) openBtn.disabled = true;
    let versions = [];
    let versionsError = null;
    try {
      const raw = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/versions`
      );
      versions = Array.isArray(raw) ? raw : [];
    } catch (error) {
      versionsError = error;
    }
    if (serial !== state.agentSessionLoadSerial || state.agentSessionProjectName !== projectName) return;
    //: A session's base is always a promoted, immutable ProjectVersion,
    //: never an implicit "current head" (same filter as the U3 run-create
    //: version dropdown).
    const promotedVersions = versions.filter(
      (version) => version && version.promotion_state === "promoted"
    );
    if (versionsError) {
      versionSelect.replaceChildren();
      versionSelect.disabled = true;
      if (help) help.textContent = "版本清單載入失敗：" + String(versionsError.message || versionsError);
    } else if (!promotedVersions.length) {
      versionSelect.replaceChildren();
      versionSelect.disabled = true;
      if (help) help.textContent = "尚無已發布版本。";
    } else {
      versionSelect.replaceChildren();
      promotedVersions.forEach((version) => {
        const commit = String(version.git_commit || "未知").slice(0, 12);
        const ref = version.git_ref || "無分支資訊";
        const created = version.created_at || "-";
        versionSelect.append(new Option(`${ref}@${commit}（${created}）`, version.id));
      });
      versionSelect.disabled = false;
      if (help) help.textContent = "此清單只列出已正式發布的版本（新到舊）。";
    }
    state.agentSessionReady = !versionsError && promotedVersions.length > 0;
    if (openBtn) openBtn.disabled = !state.agentSessionReady || state.agentSessionSubmittingOpen;
  }

  async function loadAgentSessionPanel(projectName) {
    const serial = ++state.agentSessionLoadSerial;
    state.agentSessionProjectName = projectName;
    state.agentSessionReady = false;
    state.agentSessionSubmittingOpen = false;
    agentSessionStopPolling();
    const section = element("legacy-agent-session-section");
    const stateBox = element("legacy-agent-session-state");
    if (!section) return;
    agentSessionShowPanel(null);
    if (stateBox) stateBox.textContent = "正在載入工作階段…";
    try {
      const data = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/agent-sessions`
      );
      if (serial !== state.agentSessionLoadSerial || state.agentSessionProjectName !== projectName) return;
      section.hidden = false;
      const current = data && data.current ? data.current : null;
      state.agentSessionCurrent = current;
      if (current && current.status === "active") {
        state.agentSessionPendingApprovalId = null;
        if (stateBox) {
          stateBox.textContent = `工作階段進行中（使用 ${current.provider_id || "claude-code"}；建立於 ${current.created_at || "-"}）`;
        }
        agentSessionShowPanel("legacy-agent-session-workbench");
        agentSessionRenderWorkbench(current);
        agentSessionLoadMessages(projectName, serial);
      } else if (current && current.status === "pending") {
        if (stateBox) stateBox.textContent = "工作階段準備中，狀態為 pending，稍後重新整理查看。";
        agentSessionShowPanel("legacy-agent-session-pending-panel");
        const text = element("legacy-agent-session-pending-text");
        if (text) text.textContent = "工作階段準備中，請稍後按「重新整理」查看最新狀態。";
      } else if (state.agentSessionPendingApprovalId != null) {
        if (stateBox) stateBox.textContent = "等待你到核准頁批准。";
        agentSessionShowPanel("legacy-agent-session-pending-panel");
        agentSessionRenderPendingGuidance();
      } else {
        if (stateBox) stateBox.textContent = "尚未開啟工作階段，選擇一個已發布的版本後即可開啟工作階段。";
        agentSessionShowPanel("legacy-agent-session-open-panel");
        await agentSessionLoadOpenForm(projectName, serial);
      }
    } catch (error) {
      if (serial !== state.agentSessionLoadSerial || state.agentSessionProjectName !== projectName) return;
      if (error && error.status === 404) {
        // AGENT_SESSION_V1_ENABLED is off: hide the whole section rather
        // than show a "feature disabled" state (CV-6 precedent).
        section.hidden = true;
        return;
      }
      section.hidden = false;
      agentSessionShowPanel(null);
      if (stateBox) stateBox.textContent = "工作階段載入失敗：" + (error instanceof Error ? error.message : String(error));
    }
  }

  async function submitAgentSessionOpen(event) {
    event.preventDefault();
    if (state.agentSessionSubmittingOpen || !state.agentSessionReady) return;
    const projectName = state.agentSessionProjectName;
    if (!projectName) return;
    const versionSelect = element("legacy-agent-session-version");
    const openBtn = element("legacy-agent-session-open-btn");
    const stateBox = element("legacy-agent-session-state");
    const baseVersionId = versionSelect ? versionSelect.value : "";
    if (!baseVersionId) return;
    state.agentSessionSubmittingOpen = true;
    if (openBtn) openBtn.disabled = true;
    if (stateBox) stateBox.textContent = "正在送出開啟請求…";
    try {
      const approval = await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/agent-session-open-requests`,
        { base_version_id: baseVersionId }
      );
      if (state.agentSessionProjectName !== projectName) return;
      state.agentSessionPendingApprovalId = approval && approval.id != null ? approval.id : null;
      agentSessionShowPanel("legacy-agent-session-pending-panel");
      agentSessionRenderPendingGuidance();
    } catch (error) {
      if (state.agentSessionProjectName !== projectName) return;
      if (stateBox) stateBox.textContent = "開啟 Session 失敗：" + (error instanceof Error ? error.message : String(error));
      agentSessionShowPanel("legacy-agent-session-open-panel");
    } finally {
      if (state.agentSessionProjectName === projectName) {
        state.agentSessionSubmittingOpen = false;
        if (openBtn) openBtn.disabled = !state.agentSessionReady;
      }
    }
  }

  async function agentSessionCloseCurrent() {
    const session = state.agentSessionCurrent;
    const projectName = state.agentSessionProjectName;
    if (!session || !projectName || state.agentSessionClosing) return;
    if (!window.confirm("確定要結束這個工作階段嗎？結束後無法復原，但工作區與紀錄會保留。")) return;
    state.agentSessionClosing = true;
    const closeBtn = element("legacy-agent-session-close-btn");
    if (closeBtn) closeBtn.disabled = true;
    try {
      await productMutation(
        `/api/v2/agent-sessions/${encodeURIComponent(session.id)}/close`,
        {},
        { idempotency: false }
      );
      if (state.agentSessionProjectName !== projectName) return;
      agentSessionStopPolling();
      state.agentSessionPendingApprovalId = null;
      await loadAgentSessionPanel(projectName);
    } catch (error) {
      if (state.agentSessionProjectName !== projectName) return;
      const statusBox = element("legacy-agent-session-status");
      if (statusBox) statusBox.textContent = "關閉失敗：" + (error instanceof Error ? error.message : String(error));
    } finally {
      state.agentSessionClosing = false;
      if (closeBtn) closeBtn.disabled = false;
    }
  }

  async function submitAgentSessionMessage(event) {
    event.preventDefault();
    const session = state.agentSessionCurrent;
    if (!session || state.agentSessionSubmittingMessage) return;
    const projectName = state.agentSessionProjectName;
    if (!projectName) return;
    const input = element("legacy-agent-session-input");
    const statusBox = element("legacy-agent-session-status");
    const content = input ? input.value : "";
    if (!content || !content.trim()) {
      if (statusBox) statusBox.textContent = "請先輸入訊息內容。";
      return;
    }
    //: 64 KiB cap client-side too, mirroring the server's
    //: `AGENT_SESSION_MESSAGE_MAX_BYTES` 400 -- fail fast without a round trip.
    if (new TextEncoder().encode(content).length > window.WorkspaceUI.AGENT_SESSION_MESSAGE_MAX_BYTES) {
      if (statusBox) statusBox.textContent = `訊息超過 ${window.WorkspaceUI.AGENT_SESSION_MESSAGE_MAX_BYTES} bytes 上限。`;
      return;
    }
    state.agentSessionSubmittingMessage = true;
    agentSessionSetComposerEnabled(false);
    if (statusBox) statusBox.textContent = "正在啟動 turn…";
    try {
      const result = await productMutation(
        `/api/v2/agent-sessions/${encodeURIComponent(session.id)}/messages`,
        { content }
      );
      if (state.agentSessionProjectName !== projectName) return;
      if (result && result.status === "unreachable") {
        if (statusBox) {
          statusBox.textContent = result.detail || "無法連線到 Runner，session 維持 active，可稍後重試。";
        }
        agentSessionSetComposerEnabled(true);
        return;
      }
      if (result && result.user_message) agentSessionAppendMessage(result.user_message);
      if (input) input.value = "";
      if (statusBox) statusBox.textContent = "";
      // Composer stays disabled while this turn runs -- re-enabled once
      // `agentSessionPollOnce` settles and refreshes the turn counter.
      agentSessionStartTurnPolling(session.id, result.turn_no);
    } catch (error) {
      if (state.agentSessionProjectName !== projectName) return;
      // 409 covers both "a turn is already running" and "max_turns reached"
      // (session-capacity conflicts, not client input errors) -- rendered as
      // readable status text, not a thrown/blocking error.
      if (statusBox) statusBox.textContent = "送出失敗：" + (error instanceof Error ? error.message : String(error));
      agentSessionSetComposerEnabled(true);
    } finally {
      if (state.agentSessionProjectName === projectName) state.agentSessionSubmittingMessage = false;
    }
  }

  function agentSessionTurnPanelReset() {
    const panel = element("legacy-agent-session-turn-panel");
    const events = element("legacy-agent-session-events");
    const statusEl = element("legacy-agent-session-turn-status");
    if (panel) panel.hidden = false;
    if (events) events.replaceChildren();
    if (statusEl) statusEl.textContent = "這一輪執行中…";
    state.agentSessionTurnRawEvents = [];
    const techJson = element("legacy-agent-session-turn-tech-json");
    if (techJson) techJson.textContent = "";
    const techDetails = element("legacy-agent-session-turn-tech-details");
    if (techDetails) techDetails.open = false;
    agentSessionSetLiveStatus("Claude 正在思考…");
  }

  function agentSessionAppendEvent(text) {
    const events = element("legacy-agent-session-events");
    if (!events || !text) return;
    events.append(node("p", text));
    events.scrollTop = events.scrollHeight;
  }

  //: Technical details (raw transcript-line JSON, turn numbers, byte
  //: offsets) live only inside the default-closed 「技術細節」<details> --
  //: nothing here is deleted, it is only kept out of the primary reading
  //: surface (product plan §17).
  function agentSessionRecordTurnTechDetail(event) {
    state.agentSessionTurnRawEvents.push(event);
    const techJson = element("legacy-agent-session-turn-tech-json");
    if (!techJson) return;
    let serialized;
    try {
      serialized = JSON.stringify(state.agentSessionTurnRawEvents, null, 2);
    } catch (error) {
      serialized = "（技術細節無法序列化）";
    }
    const cap = window.WorkspaceUI.AGENT_SESSION_TECH_DETAILS_MAX_CHARS;
    techJson.textContent = serialized.length > cap
      ? serialized.slice(0, cap) + "\n…（已截斷）"
      : serialized;
  }

  function agentSessionRenderTranscriptChunk(chunk) {
    String(chunk).split("\n").forEach((line) => {
      const event = window.WorkspaceUI.agentSessionParseTranscriptLine(line);
      if (!event || typeof event !== "object") return;
      // Raw parsed line goes straight into the collapsed 技術細節 record,
      // regardless of whether it also produces a friendly feed entry below.
      agentSessionRecordTurnTechDetail(event);
      const content = event.message && Array.isArray(event.message.content) ? event.message.content : null;
      if (!content) return;
      if (event.type === "assistant") {
        content.forEach((block) => {
          if (!block || typeof block !== "object") return;
          if (block.type === "text" && typeof block.text === "string" && block.text) {
            agentSessionAppendEvent(block.text);
          } else if (block.type === "tool_use") {
            const name = typeof block.name === "string" ? block.name : "tool";
            agentSessionAppendEvent(window.WorkspaceUI.agentSessionFriendlyToolEventText(name, block.input));
            agentSessionSetLiveStatus(window.WorkspaceUI.agentSessionLiveStatusForTool(name, block.input));
          }
        });
      } else if (event.type === "user") {
        content.forEach((block) => {
          if (!block || typeof block !== "object" || block.type !== "tool_result") return;
          const raw = typeof block.content === "string"
            ? block.content
            : window.WorkspaceUI.agentSessionSummarizeToolInput(block.content);
          const cap = window.WorkspaceUI.AGENT_SESSION_TOOL_RESULT_MAX_CHARS;
          agentSessionAppendEvent(raw.length > cap ? raw.slice(0, cap) + "…" : raw);
        });
      }
      // Any other line shape (`system`, `result`, unrecognized) is silently
      // skipped -- defensive parsing, never renders raw JSON to the DOM.
    });
  }

  async function agentSessionRefreshTurnCounter(sessionId) {
    const projectName = state.agentSessionProjectName;
    if (!projectName) return;
    try {
      const data = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/agent-sessions`
      );
      if (state.agentSessionProjectName !== projectName) return;
      const current = data && data.current ? data.current : null;
      if (current && current.id === sessionId) {
        state.agentSessionCurrent = current;
        agentSessionRenderWorkbench(current);
      }
    } catch (error) {
      // Best-effort only -- the turn already settled locally via the
      // transcript response; a failed refresh just leaves the turn counter
      // stale until the next manual refresh.
    }
  }

  async function agentSessionPollOnce(sessionId, turnNo, serial) {
    if (serial !== state.agentSessionPollSerial) return;
    const statusEl = element("legacy-agent-session-turn-status");
    try {
      const data = await productRead(
        `/api/v2/agent-sessions/${encodeURIComponent(sessionId)}/transcript?turn=${turnNo}&offset=${state.agentSessionPollOffset}`
      );
      if (serial !== state.agentSessionPollSerial) return;
      if (typeof data.transcript_chunk === "string" && data.transcript_chunk) {
        agentSessionRenderTranscriptChunk(data.transcript_chunk);
        state.agentSessionPollOffset += new TextEncoder().encode(data.transcript_chunk).length;
      }
      if (data.status === "unreachable") {
        if (statusEl) statusEl.textContent = "Runner 暫時無法連線，session 維持 active，稍後自動重試。";
        agentSessionSetLiveStatus("連不上執行機，稍後再試。");
      } else if (data.status === "done" || data.status === "failed" || data.status === "interrupted") {
        if (statusEl) {
          statusEl.textContent = {
            done: "這一輪已完成。",
            failed: `這一輪失敗（${data.detail || "無詳細訊息"}）。`,
            interrupted: `這一輪被中斷（${data.detail || "無詳細訊息"}）。`,
          }[data.status];
        }
        agentSessionSetLiveStatus(
          {
            done: "回合完成，等你回覆。",
            failed: "這一輪失敗了，可以再送一次訊息重試。",
            interrupted: "這一輪被中斷了，可以再送一次訊息重試。",
          }[data.status]
        );
        if (data.message) agentSessionAppendMessage(data.message);
        agentSessionStopPolling();
        await agentSessionRefreshTurnCounter(sessionId);
        return;
      } else {
        if (statusEl) statusEl.textContent = "這一輪執行中…";
      }
    } catch (error) {
      if (serial !== state.agentSessionPollSerial) return;
      if (statusEl) statusEl.textContent = "輪詢 transcript 失敗：" + (error instanceof Error ? error.message : String(error));
    }
    if (serial !== state.agentSessionPollSerial) return;
    state.agentSessionPollTimer = window.setTimeout(
      () => agentSessionPollOnce(sessionId, turnNo, serial),
      window.WorkspaceUI.AGENT_SESSION_POLL_INTERVAL_MS
    );
  }

  function agentSessionStartTurnPolling(sessionId, turnNo) {
    // `agentSessionStopPolling()` bumps the serial first, so a stale
    // scheduled callback from a previous turn/pane can never race this one
    // -- at most one poll loop is ever live (serial guard).
    agentSessionStopPolling();
    const serial = state.agentSessionPollSerial;
    state.agentSessionPollOffset = 0;
    agentSessionTurnPanelReset();
    agentSessionPollOnce(sessionId, turnNo, serial);
  }

  function agentSessionRenderDiff(response) {
    const box = element("legacy-agent-session-diff");
    if (!box) return;
    box.hidden = false;
    box.replaceChildren();
    if (!response) return;
    if (response.withheld === true) {
      box.append(node("p", "修改內容因安全政策而整段隱藏；瀏覽器不會顯示回應中的其他欄位。", "section-note"));
      return;
    }
    if (response.status === "no_workspace") {
      box.append(node("p", "工作區尚未建立（還沒有任何一輪執行過）。", "section-note"));
      return;
    }
    if (response.status === "unreachable") {
      box.append(node("p", "Runner 暫時無法連線，無法讀取修改內容；不代表工作區有問題。", "section-note"));
      return;
    }
    if (response.available !== true || typeof response.patch !== "string") {
      box.append(node("p", "修改內容的安全投影不完整，內容維持隱藏。", "section-note"));
      return;
    }
    if (response.summary) box.append(node("p", response.summary, "section-note"));
    box.append(node("pre", response.patch || "（沒有修改內容）", "task-diff-viewer"));
    const dirty = Array.isArray(response.dirty_files) ? response.dirty_files : [];
    const untracked = Array.isArray(response.untracked_files) ? response.untracked_files : [];
    const fileList = (label, paths) => {
      if (!paths.length) return;
      box.append(node("strong", label));
      const list = document.createElement("ul");
      paths.forEach((path) => list.append(node("li", path)));
      box.append(list);
    };
    fileList("已修改的檔案", dirty);
    fileList("新增未追蹤的檔案", untracked);
    if (response.truncated) box.append(node("p", "修改內容已截斷。", "section-note"));
    if (response.redacted) box.append(node("p", "修改內容已套用遮罩。", "section-note"));
  }

  async function loadAgentSessionDiff() {
    const session = state.agentSessionCurrent;
    if (!session || state.agentSessionDiffLoading) return;
    const sessionId = session.id;
    state.agentSessionDiffLoading = true;
    const box = element("legacy-agent-session-diff");
    if (box) { box.hidden = false; box.replaceChildren(node("p", "正在載入修改內容…", "section-note")); }
    try {
      const response = await productRead(`/api/v2/agent-sessions/${encodeURIComponent(sessionId)}/diff`);
      if (!state.agentSessionCurrent || state.agentSessionCurrent.id !== sessionId) return;
      agentSessionRenderDiff(response);
    } catch (error) {
      if (!state.agentSessionCurrent || state.agentSessionCurrent.id !== sessionId) return;
      if (box) {
        box.replaceChildren(node(
          "p",
          "修改內容載入失敗：" + (error instanceof Error ? error.message : String(error)),
          "section-note"
        ));
      }
    } finally {
      state.agentSessionDiffLoading = false;
    }
  }

  // -------------------------------------------------------------------
  // DG-AGENT-SESSION-CHECKPOINT: checkpoint-request -> pending guidance ->
  // (once the Approvals page decides) Promote-in-place -> pending-promote
  // guidance -> promoted. Decision-making itself stays on the Approvals
  // page; this pane only requests and polls the EXISTING
  // `GET /api/v2/approvals?kind=...` list route (no new backend read
  // surface) to display state. `note` on an approved
  // `agent_session_checkpoint` approval always contains
  // `task_id=<bridge engineering task id>`.
  // -------------------------------------------------------------------

  function agentSessionSetCheckpointStatusText(text) {
    const box = element("legacy-agent-session-checkpoint-status");
    if (box) box.textContent = text;
  }

  async function agentSessionCheckpointPollOnce(sessionId, approvalId, serial) {
    if (serial !== state.agentSessionCheckpointPollSerial) return;
    try {
      const approvals = await productRead("/api/v2/approvals?kind=agent_session_checkpoint");
      if (serial !== state.agentSessionCheckpointPollSerial) return;
      const items = (approvals && Array.isArray(approvals.items)) ? approvals.items : [];
      const match = items.find((item) => item && item.id === approvalId);
      if (match && match.status === "approved") {
        state.agentSessionCheckpointStatus = "approved";
        state.agentSessionCheckpointBridgeTaskId = window.WorkspaceUI.agentSessionExtractBridgeTaskId(match.note);
        agentSessionSetCheckpointStatusText(
          state.agentSessionCheckpointBridgeTaskId
            ? `已核准（#${approvalId}），候選版本已建立，可以按「正式發布為新版本」。`
            : `已核准（#${approvalId}），但找不到候選版本編號，請至「核准」頁確認。`
        );
        agentSessionSetLiveStatus("候選版本已核准，可以按「正式發布為新版本」。");
        agentSessionCheckpointStopPolling();
        agentSessionUpdateCheckpointButtons();
        return;
      }
      if (match && match.status === "rejected") {
        state.agentSessionCheckpointStatus = "rejected";
        agentSessionSetCheckpointStatusText(`打包請求被拒絕（#${approvalId}）：${match.note || "無詳細原因"}`);
        agentSessionSetLiveStatus("打包候選版本的請求被拒絕，可以修改後再試一次。");
        agentSessionCheckpointStopPolling();
        agentSessionUpdateCheckpointButtons();
        return;
      }
      agentSessionSetCheckpointStatusText(`已建立打包候選版本的核准請求 #${approvalId}。永不自動核准；請至「核准」頁審核。`);
      agentSessionSetLiveStatus("等待你到核准頁批准。");
    } catch (error) {
      if (serial !== state.agentSessionCheckpointPollSerial) return;
      agentSessionSetCheckpointStatusText("查詢核准狀態失敗：" + (error instanceof Error ? error.message : String(error)));
    }
    if (serial !== state.agentSessionCheckpointPollSerial) return;
    state.agentSessionCheckpointPollTimer = window.setTimeout(
      () => agentSessionCheckpointPollOnce(sessionId, approvalId, serial),
      window.WorkspaceUI.AGENT_SESSION_POLL_INTERVAL_MS
    );
  }

  async function submitAgentSessionCheckpoint() {
    const session = state.agentSessionCurrent;
    if (!session || state.agentSessionCheckpointBusy) return;
    if (!window.confirm(
      "確定要把這次修改打包成候選版本嗎？這一步會封裝、驗證目前的修改，仍需要你到「核准」頁確認後才會實際執行。"
    )) return;
    state.agentSessionCheckpointBusy = true;
    agentSessionUpdateCheckpointButtons();
    agentSessionSetCheckpointStatusText("正在送出打包請求…");
    try {
      const approval = await productMutation(
        `/api/v2/agent-sessions/${encodeURIComponent(session.id)}/checkpoint-requests`,
        {}
      );
      if (state.agentSessionCurrent !== session) return;
      state.agentSessionCheckpointApprovalId = approval && approval.id != null ? approval.id : null;
      state.agentSessionCheckpointStatus = "pending";
      state.agentSessionCheckpointBridgeTaskId = null;
      agentSessionSetLiveStatus("等待你到核准頁批准。");
      agentSessionCheckpointStopPolling();
      const serial = state.agentSessionCheckpointPollSerial;
      agentSessionCheckpointPollOnce(session.id, state.agentSessionCheckpointApprovalId, serial);
    } catch (error) {
      if (state.agentSessionCurrent !== session) return;
      agentSessionSetCheckpointStatusText("建立打包請求失敗：" + (error instanceof Error ? error.message : String(error)));
    } finally {
      if (state.agentSessionCurrent === session) {
        state.agentSessionCheckpointBusy = false;
        agentSessionUpdateCheckpointButtons();
      }
    }
  }

  async function agentSessionPromotePollOnce(approvalId, serial) {
    if (serial !== state.agentSessionPromotePollSerial) return;
    try {
      const approvals = await productRead("/api/v2/approvals?kind=engineering_task_promote");
      if (serial !== state.agentSessionPromotePollSerial) return;
      const items = (approvals && Array.isArray(approvals.items)) ? approvals.items : [];
      const match = items.find((item) => item && item.id === approvalId);
      if (match && match.status === "approved") {
        state.agentSessionPromoteStatus = "approved";
        agentSessionSetCheckpointStatusText(`已核准（#${approvalId}），新版本已建立，請至專案版本紀錄查看。`);
        agentSessionSetLiveStatus("已正式發布為新版本。");
        agentSessionPromoteStopPolling();
        agentSessionUpdateCheckpointButtons();
        return;
      }
      if (match && match.status === "rejected") {
        state.agentSessionPromoteStatus = "rejected";
        agentSessionSetCheckpointStatusText(`發布請求被拒絕（#${approvalId}）：${match.note || "無詳細原因"}`);
        agentSessionSetLiveStatus("正式發布的請求被拒絕，可以再試一次。");
        agentSessionPromoteStopPolling();
        agentSessionUpdateCheckpointButtons();
        return;
      }
      agentSessionSetCheckpointStatusText(`已建立正式發布的核准請求 #${approvalId}。永不自動核准；請至「核准」頁審核。`);
      agentSessionSetLiveStatus("等待你到核准頁批准。");
    } catch (error) {
      if (serial !== state.agentSessionPromotePollSerial) return;
      agentSessionSetCheckpointStatusText("查詢核准狀態失敗：" + (error instanceof Error ? error.message : String(error)));
    }
    if (serial !== state.agentSessionPromotePollSerial) return;
    state.agentSessionPromotePollTimer = window.setTimeout(
      () => agentSessionPromotePollOnce(approvalId, serial),
      window.WorkspaceUI.AGENT_SESSION_POLL_INTERVAL_MS
    );
  }

  async function submitAgentSessionPromote() {
    const session = state.agentSessionCurrent;
    const taskId = state.agentSessionCheckpointBridgeTaskId;
    if (!session || !taskId || state.agentSessionPromoteBusy) return;
    if (!window.confirm(
      "確定要把這個候選版本正式發布為新版本嗎？這一步只會鎖定內容（commit），仍需要具核准權限的人到「核准」頁確認後才會真的成為新版本。"
    )) return;
    state.agentSessionPromoteBusy = true;
    agentSessionUpdateCheckpointButtons();
    agentSessionSetCheckpointStatusText("正在送出發布請求…");
    try {
      const approval = await productMutation(
        `/api/v2/engineering-tasks/${encodeURIComponent(taskId)}/promote-requests`,
        {}
      );
      if (state.agentSessionCurrent !== session) return;
      state.agentSessionPromoteApprovalId = approval && approval.id != null ? approval.id : null;
      state.agentSessionPromoteStatus = "pending";
      agentSessionSetLiveStatus("等待你到核准頁批准。");
      agentSessionPromoteStopPolling();
      const serial = state.agentSessionPromotePollSerial;
      agentSessionPromotePollOnce(state.agentSessionPromoteApprovalId, serial);
    } catch (error) {
      if (state.agentSessionCurrent !== session) return;
      agentSessionSetCheckpointStatusText("建立發布請求失敗：" + (error instanceof Error ? error.message : String(error)));
    } finally {
      if (state.agentSessionCurrent === session) {
        state.agentSessionPromoteBusy = false;
        agentSessionUpdateCheckpointButtons();
      }
    }
  }

  // -------------------------------------------------------------------
  // DG-CONVERSATION-V1 CV-2a/CV-5: per-project "AI Engineer" conversation.
  // Non-streaming -- one request/response turn, mirroring `POST
  // /agent/chat`'s existing contract. `PROJECT_CONVERSATION_V1_ENABLED` off
  // makes the first `GET .../conversation` 404; that hides the whole
  // section rather than showing a "feature disabled" state (CV-6).
  // -------------------------------------------------------------------

  function aiConversationSetControlsEnabled(enabled) {
    const allow = Boolean(enabled) && state.aiConversationReady && !state.aiConversationSubmitting;
    const input = element("legacy-ai-conversation-input");
    const sendBtn = element("legacy-ai-conversation-send-btn");
    if (input) input.disabled = !allow;
    if (sendBtn) sendBtn.disabled = !allow;
  }

  function aiConversationRenderMessages(messages) {
    const list = element("legacy-ai-conversation-messages");
    if (!list) return;
    list.replaceChildren();
    if (!messages.length) {
      list.append(emptyState("尚無訊息", "輸入一句話開始這個專案的對話。"));
      return;
    }
    messages.forEach((message) => {
      const item = node("div", null, `pd-ai-message pd-ai-message-${message.role === "user" ? "user" : "assistant"}`);
      item.append(node("strong", message.role === "user" ? "你" : "AI"), node("p", message.content));
      if (message.refs && message.refs.approval_id !== undefined && message.refs.approval_id !== null) {
        item.append(node("p", `已建立待核准請求 #${message.refs.approval_id}（前往「核准」區審核）`, "section-note"));
      }
      list.append(item);
    });
    list.scrollTop = list.scrollHeight;
  }

  function resetAIConversationPanel() {
    state.aiConversationLoadSerial += 1;
    state.aiConversationProjectName = null;
    state.aiConversationSubmitting = false;
    state.aiConversationReady = false;
    const section = element("legacy-ai-conversation-section");
    if (section) section.hidden = true;
    const content = element("legacy-ai-conversation-content");
    if (content) content.hidden = true;
    const form = element("legacy-ai-conversation-form");
    if (form) form.reset();
    const messages = element("legacy-ai-conversation-messages");
    if (messages) messages.replaceChildren();
    const statusBox = element("legacy-ai-conversation-status");
    if (statusBox) statusBox.textContent = "";
    aiConversationSetControlsEnabled(false);
  }

  async function loadAIConversationPanel(projectName) {
    const serial = ++state.aiConversationLoadSerial;
    state.aiConversationProjectName = projectName;
    state.aiConversationSubmitting = false;
    state.aiConversationReady = false;
    const section = element("legacy-ai-conversation-section");
    const content = element("legacy-ai-conversation-content");
    const stateBox = element("legacy-ai-conversation-state");
    if (!section) return;
    if (content) content.hidden = true;
    aiConversationSetControlsEnabled(false);
    if (stateBox) stateBox.textContent = "正在載入 AI 工程助理對話…";
    try {
      const data = await productRead(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/conversation`
      );
      if (serial !== state.aiConversationLoadSerial || state.aiConversationProjectName !== projectName) return;
      section.hidden = false;
      state.aiConversationReady = true;
      aiConversationRenderMessages((data && data.messages) || []);
      if (content) content.hidden = false;
      aiConversationSetControlsEnabled(true);
    } catch (error) {
      if (serial !== state.aiConversationLoadSerial || state.aiConversationProjectName !== projectName) return;
      if (error && error.status === 404) {
        // PROJECT_CONVERSATION_V1_ENABLED is off: hide the whole section,
        // not an error card (CV-6 -- data is retained, only the entry point
        // is hidden).
        section.hidden = true;
        return;
      }
      section.hidden = false;
      if (stateBox) stateBox.textContent = "AI 工程助理對話載入失敗：" + (error instanceof Error ? error.message : String(error));
    }
  }

  async function submitAIConversationMessage(event) {
    event.preventDefault();
    if (state.aiConversationSubmitting || !state.aiConversationReady) return;
    const projectName = state.aiConversationProjectName;
    if (!projectName) return;
    const input = element("legacy-ai-conversation-input");
    const statusBox = element("legacy-ai-conversation-status");
    const content = input ? input.value : "";
    if (!content || !content.trim()) {
      if (statusBox) statusBox.textContent = "請先輸入訊息內容。";
      return;
    }
    state.aiConversationSubmitting = true;
    aiConversationSetControlsEnabled(false);
    if (statusBox) statusBox.textContent = "正在送出…";
    try {
      const result = await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/conversation/messages`,
        { content }
      );
      if (state.aiConversationProjectName !== projectName) return;
      if (result && result.status === "llm_unavailable") {
        if (statusBox) statusBox.textContent = result.detail || "尚未設定 ANTHROPIC_API_KEY，對話功能未啟用";
        return;
      }
      // input 不清空的例外：llm_unavailable 是 degraded 狀態，保留使用者輸入
      // 內容以便設定完成後直接重送；成功時才清空。
      if (input) input.value = "";
      // Reload the bounded history from the server rather than hand-splicing
      // local state -- SQLite remains the single source of truth for what
      // is shown (CV-1).
      await loadAIConversationPanel(projectName);
      if (statusBox) statusBox.textContent = "";
    } catch (error) {
      if (state.aiConversationProjectName !== projectName) return;
      if (statusBox) statusBox.textContent = "送出失敗：" + (error instanceof Error ? error.message : String(error));
    } finally {
      if (state.aiConversationProjectName === projectName) {
        state.aiConversationSubmitting = false;
        aiConversationSetControlsEnabled(true);
      }
    }
  }

  // ---------------------------------------------------------------------
  // 資料集（legacy 相容，DG-UI-UNIFICATION v1 U5）：`/api/v2/legacy-datasets*`
  // -- name@version 卡片清單、資料卡查看／更新、註冊表單。與既有 workspace
  // 「Dataset Assets」（v2 typed，`section-datasets`）分開、互不影響。
  // ---------------------------------------------------------------------

  async function loadLegacyDatasets() {
    element("legacy-dataset-state").textContent = "正在載入資料集…";
    try {
      const datasets = await productRead("/api/v2/legacy-datasets");
      state.legacyDatasets = Array.isArray(datasets) ? datasets : [];
      state.legacyDatasetsLoaded = true;
      element("legacy-dataset-state").textContent = `共 ${state.legacyDatasets.length} 個資料集版本。`;
      renderLegacyDatasets();
    } catch (error) {
      element("legacy-dataset-state").textContent = "無法載入資料集："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderLegacyDatasets() {
    const container = element("legacy-dataset-list");
    container.replaceChildren();
    if (!state.legacyDatasets.length) {
      container.append(emptyState("尚無已註冊的資料集", "使用下方表單註冊第一個版本。"));
      return;
    }
    for (const dataset of state.legacyDatasets) {
      const card = node("article", null, "item-card");
      const header = node("div", null, "item-card-header");
      header.append(node("h3", `${dataset.name}@${dataset.version}`));
      if (!dataset.card) header.append(node("span", "無資料卡", "honesty-label"));
      card.append(header);
      const descriptionFirstLine = dataset.card && dataset.card.description
        ? String(dataset.card.description).split("\n")[0]
        : "（尚未登記資料卡）";
      card.append(node("p", descriptionFirstLine));
      const meta = node("div", null, "item-meta");
      meta.append(
        node("span", `${dataset.file_count == null ? "unknown" : dataset.file_count} 個檔案`),
        node("span", `${dataset.size_bytes == null ? "unknown" : dataset.size_bytes} bytes`)
      );
      if (dataset.card && dataset.card.derived_from) {
        meta.append(node("span", `衍生自 ${dataset.card.derived_from.name}@${dataset.card.derived_from.version}`));
      }
      card.append(meta);
      const actions = node("div", null, "button-row");
      const viewCard = node("button", "檢視資料卡", "button button-quiet");
      viewCard.type = "button";
      viewCard.addEventListener("click", () => openLegacyDatasetCard(dataset.name, dataset.version));
      actions.append(viewCard);
      card.append(actions);
      container.append(card);
    }
  }

  async function openLegacyDatasetCard(name, version) {
    state.legacyDatasetCardKey = `${name}@${version}`;
    element("legacy-dataset-card-panel").hidden = false;
    element("legacy-dataset-card-title").textContent = `資料卡 · ${name}@${version}`;
    try {
      const card = await productRead(
        `/api/v2/legacy-datasets/${encodeURIComponent(name)}/${encodeURIComponent(version)}/card`
      );
      state.legacyDatasetCard = card;
      element("legacy-dataset-card-summary").textContent = card.note || "查看原始內容";
      element("legacy-dataset-card-rendered").textContent = card.rendered || "";
      element("legacy-dataset-card-description-input").value = (card.card && card.card.description) || "";
      element("legacy-dataset-card-method-input").value = (card.card && card.card.method) || "";
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "無法載入資料卡");
    }
  }

  function closeLegacyDatasetCard() {
    state.legacyDatasetCardKey = null;
    state.legacyDatasetCard = null;
    element("legacy-dataset-card-panel").hidden = true;
  }

  async function submitLegacyDatasetCardUpdate() {
    const key = state.legacyDatasetCardKey;
    if (!key) return;
    const [name, version] = key.split("@");
    const status = element("legacy-dataset-card-update-status");
    const description = element("legacy-dataset-card-description-input").value.trim();
    const method = element("legacy-dataset-card-method-input").value.trim();
    if (!description || !method) {
      status.textContent = "說明與製作方式皆為必填。";
      return;
    }
    try {
      await productMutation(
        `/api/v2/legacy-datasets/${encodeURIComponent(name)}/${encodeURIComponent(version)}/card`,
        { description, method },
        { method: "PATCH" }
      );
      status.textContent = "資料卡已更新。";
      await Promise.all([openLegacyDatasetCard(name, version), loadLegacyDatasets()]);
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "更新失敗";
    }
  }

  async function submitLegacyDatasetCreate() {
    const status = element("legacy-dataset-create-status");
    const name = element("legacy-dataset-create-name").value.trim();
    const version = element("legacy-dataset-create-version").value.trim();
    const sourcePath = element("legacy-dataset-create-source-path").value.trim();
    const description = element("legacy-dataset-create-description").value.trim();
    const method = element("legacy-dataset-create-method").value.trim();
    if (!name || !version || !sourcePath || !description || !method) {
      status.textContent = "名稱／版本／來源路徑／說明／製作方式皆為必填。";
      return;
    }
    try {
      await productMutation("/api/v2/legacy-datasets", {
        name,
        version,
        source_path: sourcePath,
        description,
        method,
      });
      status.textContent = `資料集 ${name}@${version} 已註冊。`;
      element("legacy-dataset-create-form").reset();
      await loadLegacyDatasets();
    } catch (error) {
      status.textContent = error instanceof Error ? error.message : "註冊失敗";
    }
  }

  function renderRuns() {
    const body = element("run-list");
    body.replaceChildren();
    const productEnabled = Boolean(
      state.workspace.capabilities
      && state.workspace.capabilities.run_experience_v2
      && state.workspace.capabilities.run_experience_v2.enabled
    );
    element("run-state").textContent = productEnabled
      ? "Recent Runs 來自 ExecutionPlan Product projection；unknown 會保留為 unknown。"
      : "Product Run Experience 未啟用；目前只顯示有限 legacy Job adapter。";
    if (!state.workspace.recent_runs.length) {
      const row = node("tr");
      const cell = node("td", "沒有 caller 可查看的 recent run。", "empty-state");
      cell.colSpan = 6;
      row.append(cell);
      body.append(row);
      renderRunDetail();
      return;
    }
    for (const run of state.workspace.recent_runs) {
      const row = node("tr");
      const isProductRun = run.source === "execution_plan_product_projection";
      const actionCell = node("td");
      if (isProductRun) {
        const open = node("button", "查看", "button button-quiet");
        open.type = "button";
        open.addEventListener("click", () => loadRunDetail(run.plan_id));
        actionCell.append(open);
      } else {
        actionCell.append(node("span", "有限 adapter", "honesty-label"));
      }
      row.append(
        node("td", String(run.plan_id || run.id)),
        node("td", String(run.project_name || "unknown")),
        node("td", String(run.contract_kind || run.type || "unknown")),
        node("td", String(run.state || run.status || "unknown")),
        node("td", formatTimestamp(run.created_at)),
        actionCell
      );
      body.append(row);
    }
    renderRunDetail();
  }

  function renderRunTimeline(detail) {
    const target = element("run-timeline");
    target.replaceChildren();
    const timeline = detail && detail.timeline;
    const items = timeline && Array.isArray(timeline.items) ? timeline.items : [];
    if (!items.length) {
      target.append(emptyState("沒有可信 timeline event", "Legacy 或 partial lineage 不會被補造。"));
      return;
    }
    for (const event of items) {
      const card = node("article", null, "timeline-item");
      card.append(
        node("strong", String(event.kind || "unknown")),
        node("span", formatTimestamp(event.timestamp))
      );
      const facts = [
        event.state ? `state ${event.state}` : null,
        event.from_state || event.to_state
          ? `${event.from_state || "-"} → ${event.to_state || "-"}`
          : null,
        event.from_liveness || event.to_liveness
          ? `liveness ${event.from_liveness || "-"} → ${event.to_liveness || "-"}`
          : null,
        event.reason_code ? `reason ${event.reason_code}` : null,
      ].filter(Boolean);
      if (facts.length) card.append(node("small", facts.join(" · ")));
      target.append(card);
    }
    if (timeline.truncated) {
      target.append(node("p", "Timeline 已依安全上限截斷。", "section-note"));
    }
  }

  function renderRunArtifacts() {
    const target = element("run-artifact-list");
    target.replaceChildren();
    const page = state.runArtifacts;
    if (!page) {
      target.append(emptyState("尚未載入 artifact metadata", "內容與下載位置不會由此 API 回傳。"));
      return;
    }
    const items = Array.isArray(page.items) ? page.items : [];
    if (!items.length) {
      const reason = page.availability === "unknown"
        ? "尚無可信 metadata，或 lineage/metadata 無法完整驗證。"
        : "目前沒有已回報的 artifact metadata。";
      target.append(emptyState("Artifact metadata 為空", reason));
    }
    for (const artifact of items) {
      const card = node("article", null, "artifact-item");
      card.append(
        node("strong", String(artifact.relative_path)),
        node("span", `${artifact.kind} · ${artifact.size_bytes} 位元組`),
        node("small", `SHA-256 ${artifact.sha256} · ${formatTimestamp(artifact.reported_at)}`)
      );
      target.append(card);
    }
    const honesty = [
      `availability ${page.availability}`,
      "僅 metadata",
      page.complete ? "完整" : "收集完整性未知",
      page.truncated ? "已截斷" : null,
    ].filter(Boolean).join(" · ");
    target.append(node("p", honesty, "section-note"));
  }

  function renderRunDetail() {
    const panel = element("run-detail-panel");
    const detail = state.runDetail;
    element("run-detail-meta").replaceChildren();
    element("run-clone-result").textContent = "尚未建立 clone preview。";
    if (!detail) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    element("run-detail-title").textContent = `Product Run · ${detail.plan_id}`;
    const statePill = element("run-detail-state");
    statePill.textContent = String(detail.state || "unknown");
    statePill.classList.toggle("attention", detail.state === "needs_attention");
    statePill.classList.toggle(
      "terminal",
      ["succeeded", "failed", "cancelled", "rejected", "blocked"].includes(detail.state)
    );
    element("run-detail-honesty").textContent = detail.contract.verified
      ? "已驗證的 ExecutionPlan v2 投影；正式 Job/attempt 證據仍為權威。"
      : "有限的 legacy 投影；unknown 維度與部分 lineage 如實保留。";
    const meta = element("run-detail-meta");
    appendDetail(meta, "專案", `${detail.project_name} · ${detail.project_id}`);
    appendDetail(meta, "契約", `${detail.contract.kind} · ${detail.contract.version || "unknown"}`);
    appendDetail(meta, "正式 Job", detail.canonical_job_status || "尚未產生");
    appendDetail(
      meta,
      "目前 attempt",
      detail.current_attempt
        ? `${detail.current_attempt.state} · liveness ${detail.current_attempt.liveness}`
        : "無"
    );
    appendDetail(
      meta,
      "注意",
      detail.attention_reasons.length ? detail.attention_reasons.join(", ") : "無"
    );
    appendDetail(meta, "建立時間", formatTimestamp(detail.created_at));
    const verified = detail.contract.kind === "execution_plan_v2" && detail.contract.verified === true;
    element("run-clone-btn").disabled = !verified;
    element("run-stop-btn").disabled = !(
      verified && detail.canonical_job_status === "running" && detail.current_attempt
    );
    renderRunTimeline(detail);
    renderRunArtifacts();
  }

  async function loadRunArtifacts(planId) {
    if (!planId) return;
    const button = element("run-artifacts-btn");
    button.disabled = true;
    element("run-artifact-list").replaceChildren(
      emptyState("正在載入 artifact metadata…", "只讀取 validated metadata。")
    );
    try {
      const page = await productRead(`/api/v2/runs/${planId}/artifacts?limit=50`);
      if (!page || page.plan_id !== planId || page.metadata_only !== true) {
        throw new Error("Artifact metadata response contract mismatch");
      }
      state.runArtifacts = page;
      renderRunArtifacts();
    } catch (error) {
      state.runArtifacts = null;
      element("run-artifact-list").replaceChildren(
        emptyState("無法載入 artifact metadata", error instanceof Error ? error.message : "未知錯誤")
      );
    } finally {
      button.disabled = false;
    }
  }

  async function loadRunDetail(planId) {
    state.selectedRunId = planId;
    state.runDetail = null;
    state.runArtifacts = null;
    state.runStopRequestKey = null;
    const panel = element("run-detail-panel");
    panel.hidden = false;
    element("run-detail-title").textContent = `Product Run · ${planId}`;
    element("run-detail-honesty").textContent = "正在載入 server-authorized projection…";
    element("run-detail-meta").replaceChildren();
    element("run-timeline").replaceChildren();
    element("run-artifact-list").replaceChildren();
    clearAlert();
    try {
      const detail = await productRead(`/api/v2/runs/${planId}`);
      if (!detail || detail.plan_id !== planId || !detail.contract) {
        throw new Error("Product Run detail response contract mismatch");
      }
      state.runDetail = detail;
      renderRunDetail();
      if (!element("run-compare-left").value) element("run-compare-left").value = planId;
      else if (!element("run-compare-right").value) element("run-compare-right").value = planId;
      await loadRunArtifacts(planId);
      panel.scrollIntoView({ block: "start" });
    } catch (error) {
      state.runDetail = null;
      panel.hidden = false;
      element("run-detail-honesty").textContent = error instanceof Error
        ? error.message
        : "無法載入 Product Run";
    }
  }

  async function previewRunClone() {
    const detail = state.runDetail;
    if (!detail) return;
    const button = element("run-clone-btn");
    button.disabled = true;
    clearAlert();
    try {
      const overrides = JSON.parse(element("run-clone-overrides").value || "{}");
      if (!overrides || Array.isArray(overrides) || typeof overrides !== "object") {
        throw new Error("Clone overrides 必須是 JSON object。");
      }
      const preview = await productMutation(
        `/api/v2/runs/${detail.plan_id}/clone-previews`,
        { parameter_overrides: overrides },
        { idempotency: false }
      );
      if (!preview || preview.source_plan_id !== detail.plan_id || preview.ready !== true) {
        throw new Error("Clone preview response contract mismatch");
      }
      element("run-clone-result").textContent =
        `Preview ready · digest ${preview.plan_digest} · 尚未建立或執行新的 Run。`;
    } catch (error) {
      element("run-clone-result").textContent = error instanceof Error
        ? error.message
        : "無法建立 Clone preview";
    } finally {
      button.disabled = false;
    }
  }

  async function requestRunStop() {
    const detail = state.runDetail;
    if (!detail) return;
    const button = element("run-stop-btn");
    button.disabled = true;
    clearAlert();
    try {
      const idempotencyKey = state.runStopRequestKey || randomUUID();
      state.runStopRequestKey = idempotencyKey;
      const result = await productMutation(
        `/api/v2/runs/${detail.plan_id}/stop-requests`,
        {},
        { idempotencyKey }
      );
      await initialize();
      activateSection("runs");
      await loadRunDetail(detail.plan_id);
      showAlert(
        `Stop approval #${result.approval_id} · ${result.status}；Job terminal status 尚未被此 request 改動。`
      );
    } catch (error) {
      button.disabled = false;
      showAlert(error instanceof Error ? error.message : "無法建立 Stop request");
    }
  }

  function renderRunComparison() {
    const target = element("run-compare-result");
    target.replaceChildren();
    const comparison = state.runComparison;
    if (!comparison) {
      target.append(node("div", "選擇兩個 Product Run 後比較；unknown 不會被補造成差異。", "inline-state"));
      return;
    }
    for (const [name, dimension] of Object.entries(comparison.dimensions || {})) {
      const card = node("article", null, "comparison-item");
      //: DG-UI-UNIFICATION v1 U2: `equal` is our own computed tri-state text
      //: (translated); `dimension.availability` stays the raw server enum
      //: (e.g. "known"/"unknown") per the JSON-simplification packet.
      const equal = dimension.equal === null || dimension.equal === undefined
        ? "未知"
        : dimension.equal ? "相同" : "不同";
      card.append(
        node("strong", name.replaceAll("_", " ")),
        node("span", `${dimension.availability} · ${equal}`)
      );
      if (dimension.availability === "known") {
        const rows = node("dl", null, "detail-list");
        appendDetail(rows, "左側", keyValueLines(dimension.left));
        appendDetail(rows, "右側", keyValueLines(dimension.right));
        card.append(rows);
        if (
          (dimension.left !== null && typeof dimension.left === "object")
          || (dimension.right !== null && typeof dimension.right === "object")
        ) {
          const raw = node("details");
          const rawSummary = node("summary", "查看原始內容");
          const pre = node("pre");
          pre.textContent = JSON.stringify({ left: dimension.left, right: dimension.right }, null, 2);
          raw.append(rawSummary, pre);
          card.append(raw);
        }
      }
      target.append(card);
    }
    if (comparison.truncated) {
      target.append(node("p", "Comparison 已依安全上限截斷。", "section-note"));
    }
  }

  async function compareRuns() {
    const left = element("run-compare-left").value.trim();
    const right = element("run-compare-right").value.trim();
    const button = element("run-compare-btn");
    button.disabled = true;
    clearAlert();
    state.runComparison = null;
    element("run-compare-result").replaceChildren(
      node("div", "正在執行唯讀比較…", "inline-state")
    );
    try {
      const query = new URLSearchParams({ left_plan_id: left, right_plan_id: right });
      const result = await productRead(`/api/v2/runs/compare?${query.toString()}`);
      if (!result || result.left_plan_id !== left || result.right_plan_id !== right) {
        throw new Error("Product Run comparison response contract mismatch");
      }
      state.runComparison = result;
      renderRunComparison();
    } catch (error) {
      element("run-compare-result").replaceChildren(
        emptyState("無法比較 Product Runs", error instanceof Error ? error.message : "未知錯誤")
      );
    } finally {
      button.disabled = false;
    }
  }

  // ---- 工作（DG-UI-UNIFICATION v1 U3）：thin `/api/v2/jobs` wrappers ------
  // Ported from `static/index.html` `renderJobs()`/`elapsed()` (:3315-3364),
  // `static/ui.js`'s job-results panel (:4591-4684), and the stop/diagnose
  // confirm flows -- retargeted at the `/api/v2/jobs*` surface throughout.
  // Business logic (which action applies, elapsed formatting, download
  // href) lives in `workspace-features.js`'s `WorkspaceUI.jobRowActions`/
  // `jobElapsed`/`jobResultDownloadHref`; this file only builds DOM nodes
  // and wires events, matching the split used for the approval summary.

  const JOB_RESULTS_METRICS_PREVIEW_MAX_BYTES = 65536;

  function renderJobDispatchProjectOptions() {
    const select = element("job-dispatch-project");
    const previous = select.value;
    select.replaceChildren();
    const none = node("option", "（無）");
    none.value = "";
    select.append(none);
    const projects = (state.workspace && state.workspace.projects) || [];
    for (const project of projects) {
      const option = node("option", project.name);
      option.value = project.name;
      select.append(option);
    }
    if (projects.some((project) => project.name === previous)) select.value = previous;
  }

  async function loadJobs() {
    element("jobs-state").textContent = "正在載入任務…";
    try {
      const jobs = await productRead("/api/v2/jobs");
      state.jobs = Array.isArray(jobs) ? jobs : [];
      state.jobsLoaded = true;
      element("jobs-state").textContent = `共 ${state.jobs.length} 筆任務。`;
      renderJobs();
    } catch (error) {
      element("jobs-state").textContent = "無法載入任務："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderJobs() {
    const body = element("jobs-tbody");
    body.replaceChildren();
    if (!state.jobs.length) {
      const row = node("tr");
      const cell = node("td", "目前沒有任務", "empty-state");
      cell.colSpan = 7;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const job of state.jobs.slice().reverse()) {
      body.append(buildJobRow(job));
    }
  }

  function buildJobRow(job) {
    const row = node("tr", null, job.stalled_suspect ? "job-row stalled" : "job-row");
    const statusCell = node("td");
    const label = window.WorkspaceUI.STATUS_LABEL[job.status] || job.status;
    statusCell.append(node("span", label, `status-pill status-${job.status}`));
    if (job.stalled_suspect) statusCell.append(node("span", "疑似卡死", "stall-badge"));

    const commandCell = node("td", null, "muted");
    const commandCode = node("code", job.command);
    commandCode.title = job.command;
    commandCell.append(commandCode);

    const actionsCell = node("td", null, "button-row");
    const actions = window.WorkspaceUI.jobRowActions(job);
    const log = node("button", "查看日誌", "button button-quiet");
    log.type = "button";
    log.addEventListener("click", () => openJobLog(job.id));
    actionsCell.append(log);
    if (actions.cancel) {
      const cancel = node("button", "取消", "button button-quiet");
      cancel.type = "button";
      cancel.addEventListener("click", () => cancelJobAction(job.id));
      actionsCell.append(cancel);
    }
    if (actions.stop) {
      const stop = node("button", "停止（可能立即執行）", "button button-quiet");
      stop.type = "button";
      stop.addEventListener("click", () => stopJobAction(job.id));
      actionsCell.append(stop);
    }
    if (actions.diagnose) {
      const diagnose = node("button", "診斷", "button button-quiet");
      diagnose.type = "button";
      diagnose.addEventListener("click", () => diagnoseJobAction(job.id));
      actionsCell.append(diagnose);
    }
    if (actions.engineering) {
      //: DG-UI-UNIFICATION v1 U6a: replaces the U3 placeholder -- navigates
      //: to the AI 工程 section and opens this job's owning task/run detail.
      //: `job.engineering_task_id` is the native task id; a job without one
      //: but with `engineering_validation_request_id` belongs to a legacy
      //: Coding Run, which the merged `/api/v2/engineering-tasks` list
      //: exposes as `legacy-coding-run-{job.source_coding_run_id}` -- if
      //: neither id is available this falls back to just opening the list.
      const jump = node("button", "查看 AI 工程任務", "button button-quiet");
      jump.type = "button";
      jump.addEventListener("click", () => {
        activateSection("engineering");
        const taskId = job.engineering_task_id
          || (job.source_coding_run_id != null ? `legacy-coding-run-${job.source_coding_run_id}` : null);
        if (taskId) openEngineeringTaskDetail(taskId);
      });
      actionsCell.append(jump);
    }

    row.append(
      node("td", `#${job.id}`),
      statusCell,
      node("td", job.project || "-"),
      node("td", job.server || "-"),
      node("td", window.WorkspaceUI.jobElapsed(job)),
      commandCell,
      actionsCell
    );
    return row;
  }

  function jobResultsSetState(kind, message) {
    const box = element("job-results-state");
    box.textContent = message;
    box.dataset.state = kind;
  }

  function resetJobResultsPanel() {
    element("job-results-list").replaceChildren();
    const metrics = element("job-results-metrics");
    metrics.hidden = true;
    metrics.textContent = "";
    jobResultsSetState("loading", "正在載入結果檔案…");
  }

  async function fetchJobResultFileText(jobId, path) {
    const href = window.WorkspaceUI.jobResultDownloadHref(jobId, path);
    const response = await fetch(href, {
      credentials: "same-origin",
      cache: "no-store",
      headers: requestHeaders(),
    });
    if (!response.ok) throw new Error(`${response.status}: ${response.statusText}`);
    return response.text();
  }

  async function loadJobResultsPanel(jobId) {
    resetJobResultsPanel();
    let data;
    try {
      data = await productRead(`/api/v2/jobs/${jobId}/results`);
    } catch (error) {
      jobResultsSetState("error", "讀取結果檔案清單失敗："
        + (error instanceof Error ? error.message : "未知錯誤"));
      return;
    }
    if (!data || data.collected === false) {
      jobResultsSetState("empty", "尚未收集到結果檔案");
      return;
    }
    const files = Array.isArray(data.files) ? data.files : [];
    if (!files.length) {
      jobResultsSetState("empty", "結果目錄存在，但沒有可下載的檔案");
      return;
    }
    jobResultsSetState(
      "ready",
      data.truncated ? "結果檔案清單（已達上限，可能未列出全部檔案）：" : "結果檔案："
    );
    const list = element("job-results-list");
    for (const file of files) {
      const item = node("li");
      const link = node("a", `${file.path}（${file.size} bytes）`);
      link.href = window.WorkspaceUI.jobResultDownloadHref(jobId, file.path);
      item.append(link);
      list.append(item);
    }
    const metricsFile = files.find((file) => file.path === "metrics.json");
    if (
      metricsFile
      && typeof metricsFile.size === "number"
      && metricsFile.size <= JOB_RESULTS_METRICS_PREVIEW_MAX_BYTES
    ) {
      const metrics = element("job-results-metrics");
      try {
        metrics.textContent = await fetchJobResultFileText(jobId, "metrics.json");
        metrics.hidden = false;
      } catch (_error) {
        // metrics.json 預覽失敗不影響檔案清單本身；使用者仍可用上面的下載連結。
        metrics.hidden = true;
      }
    }
  }

  function showJobLogPanel() {
    element("job-log-panel").hidden = false;
  }

  function closeJobLogPanel() {
    element("job-log-panel").hidden = true;
    state.openJobLogId = null;
  }

  async function openJobLog(id) {
    state.openJobLogId = id;
    element("job-log-title").textContent = `任務 #${id} log`;
    element("job-log-content").textContent = "載入中…";
    showJobLogPanel();
    resetJobResultsPanel();
    try {
      const data = await productRead(`/api/v2/jobs/${id}/log`);
      const label = window.WorkspaceUI.STATUS_LABEL[data.status] || data.status;
      element("job-log-meta").textContent = `狀態：${label}　${data.live ? "（即時抓取）" : "（記錄的 log_tail）"}`;
      element("job-log-content").textContent = data.log_tail || "（無 log 內容）";
    } catch (error) {
      element("job-log-content").textContent = "讀取失敗："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
    loadJobResultsPanel(id);
  }

  async function cancelJobAction(id) {
    try {
      await productMutation(`/api/v2/jobs/${id}/cancel`, {});
      showAlert("已取消");
      loadJobs();
    } catch (error) {
      showAlert("取消失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function stopJobAction(id) {
    if (!window.confirm(
      `要送出停止任務 #${id} 嗎？若網頁直接執行已啟用（預設），這個請求可能在同一回應內自動核准並立即停止；否則會建立待核准請求。`
    )) return;
    try {
      const result = await productMutation(`/api/v2/jobs/${id}/stop-requests`, { source: "web" });
      showAlert(result && result.auto_approved ? `已停止（任務 #${id}）` : "已建立停止請求，請到「核准」核准");
      loadJobs();
    } catch (error) {
      showAlert("建立停止請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function diagnoseJobAction(id) {
    state.openJobLogId = id;
    element("job-log-title").textContent = `任務 #${id} 失敗診斷`;
    element("job-log-meta").textContent = "只顯示診斷說明與修改建議，不會自動執行、修改程式碼或重跑任務";
    element("job-log-content").textContent = "診斷中，可能需要幾十秒…";
    showJobLogPanel();
    try {
      const data = await productMutation(`/api/v2/jobs/${id}/diagnose`, {});
      element("job-log-content").textContent = data.diagnosis || "（沒有拿到診斷內容）";
    } catch (error) {
      element("job-log-content").textContent = "診斷失敗："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  async function submitJobDispatch(event) {
    event.preventDefault();
    const command = element("job-dispatch-command").value.trim();
    const status = element("job-dispatch-status");
    if (!command) {
      status.textContent = "請先填寫指令。";
      return;
    }
    const project = element("job-dispatch-project").value || null;
    const serverMode = document.querySelector('input[name="job-dispatch-server-mode"]:checked');
    const pinServer = serverMode && serverMode.value === "named"
      ? element("job-dispatch-server-name").value.trim() || null
      : null;
    const button = element("job-dispatch-submit-btn");
    button.disabled = true;
    status.textContent = "送出中…";
    try {
      const body = { command, project, pin_server: pinServer, source: "web" };
      const result = await productMutation("/api/v2/dispatch-requests", body);
      status.textContent = result && result.auto_approved
        ? "已執行（任務已建立並可能已開始執行）。"
        : "已建立待核准的核准卡，請到「核准」核准。";
      element("job-dispatch-form").reset();
      renderJobDispatchServerFieldState();
      if (state.jobsLoaded) loadJobs();
    } catch (error) {
      status.textContent = "建立任務失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    } finally {
      button.disabled = false;
    }
  }

  function renderJobDispatchServerFieldState() {
    const serverMode = document.querySelector('input[name="job-dispatch-server-mode"]:checked');
    element("job-dispatch-server-name").disabled = !serverMode || serverMode.value !== "named";
  }

  // ---- AI 工程（DG-UI-UNIFICATION v1 U6a）：thin `/api/v2/engineering- -----
  // tasks*` / `/api/v2/coding-agents` / `/api/v2/coding-runs*` /
  // `/api/v2/legacy-projects/{name}/{engineering-task,coding-task}-
  // request*` wrappers. Ported from `static/ui.js`'s wizard
  // (`renderStructuredInstruction()` :336-421 -- reused byte-for-byte via
  // `window.WorkspaceUI.renderEngineeringTaskInstruction`) and detail-tab
  // renderers (`renderTaskOverview()`.."renderTaskActions()` :1778-2452).
  // The wizard here is a single scrollable multi-section form (matching
  // this workspace's existing `#bootstrap-form` "wizard-layout" pattern)
  // rather than the legacy paginated next/back flow -- content parity
  // (all fields, same instruction contract, same server-side validation)
  // is preserved; only the step-by-step page transition is not ported.
  // Detail-tab pane switching uses the same `data-*-tab`/`data-*-panel`
  // pairing as `legacy-project-detail-tabs` above.

  function engineeringTaskStatusCode(task) {
    const presentation = task && task.presentation;
    return (presentation && presentation.state && presentation.state.code) || "unknown";
  }

  function engineeringTaskStatusLabel(task) {
    const presentation = task && task.presentation;
    return (presentation && presentation.state && presentation.state.label) || "狀態未知";
  }

  function engineeringTaskPhaseLabel(task) {
    const presentation = task && task.presentation;
    return (presentation && presentation.phase && presentation.phase.label) || "未知";
  }

  async function loadEngineeringTasks() {
    element("engineering-tasks-state").textContent = "正在載入 AI 工程任務…";
    try {
      const tasks = await productRead("/api/v2/engineering-tasks");
      state.engineeringTasks = Array.isArray(tasks) ? tasks : [];
      state.engineeringTasksLoaded = true;
      element("engineering-tasks-state").textContent = state.engineeringTasks.length
        ? `共 ${state.engineeringTasks.length} 筆任務。`
        : "目前沒有 AI 工程任務。";
      renderEngineeringTasksTable();
    } catch (error) {
      element("engineering-tasks-state").textContent = "無法載入任務："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderEngineeringTasksTable() {
    const body = element("engineering-tasks-tbody");
    body.replaceChildren();
    if (!state.engineeringTasks.length) {
      const row = node("tr");
      const cell = node("td", "目前沒有 AI 工程任務", "empty-state");
      cell.colSpan = 5;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const task of state.engineeringTasks) {
      body.append(buildEngineeringTaskRow(task));
    }
  }

  function buildEngineeringTaskRow(task) {
    const row = node("tr", null, "job-row");
    row.style.cursor = "pointer";
    const statusCode = engineeringTaskStatusCode(task);
    const statusCell = node("td");
    statusCell.append(node("span", engineeringTaskStatusLabel(task), `status-pill status-${statusCode}`));
    statusCell.append(node("span", ` · ${engineeringTaskPhaseLabel(task)}`, "muted"));
    const idCell = node("td");
    const idCode = node("code", window.WorkspaceUI.engineeringTaskShortId(task.id));
    idCode.title = String(task.id || "");
    idCell.append(idCode);
    row.append(
      idCell,
      node("td", task.project || "-"),
      statusCell,
      node("td", task.base_commit || task.observed_base_commit || "未綁定"),
      node("td", formatTimestamp(task.updated_at))
    );
    row.addEventListener("click", () => openEngineeringTaskDetail(task.id));
    return row;
  }

  function engineeringTaskDetailTabs() {
    return Array.from(document.querySelectorAll("[data-task-tab]"));
  }

  function activateEngineeringTaskTab(tab) {
    state.engineeringDetailTab = tab;
    for (const button of engineeringTaskDetailTabs()) {
      button.classList.toggle("active", button.getAttribute("data-task-tab") === tab);
    }
    for (const panel of document.querySelectorAll("[data-task-panel]")) {
      panel.hidden = panel.getAttribute("data-task-panel") !== tab;
    }
    if (tab === "changes") loadEngineeringTaskDiff();
  }

  function closeEngineeringTaskDetail() {
    state.engineeringDetailTaskId = null;
    state.engineeringDetailTask = null;
    element("engineering-task-detail-panel").hidden = true;
  }

  async function openEngineeringTaskDetail(taskId) {
    state.engineeringDetailTaskId = taskId;
    state.engineeringDetailTask = null;
    element("engineering-task-detail-panel").hidden = false;
    element("engineering-task-detail-title").textContent = `AI 工程任務 · ${window.WorkspaceUI.engineeringTaskShortId(taskId)}`;
    element("engineering-task-detail-status").textContent = "載入中…";
    try {
      const task = await productRead(`/api/v2/engineering-tasks/${encodeURIComponent(taskId)}`);
      if (state.engineeringDetailTaskId !== taskId) return;
      state.engineeringDetailTask = task;
      renderEngineeringTaskDetail(task);
    } catch (error) {
      if (state.engineeringDetailTaskId !== taskId) return;
      element("engineering-task-detail-status").textContent = "載入失敗";
      showAlert("載入 AI 工程任務失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  function detailHeading(panel, text) {
    panel.append(node("h4", text));
  }

  function emptyDetailState(panel, text) {
    panel.append(node("p", text, "section-note"));
  }

  function renderEngineeringOverviewTab(task) {
    const panel = element("engineering-task-pane-overview");
    panel.replaceChildren();
    detailHeading(panel, "任務總覽");
    const structured = task.structured_request || {};
    const objective = node("p", structured.objective || task.instruction || "（無 objective）");
    panel.append(objective);
    const grid = detailListNode([
      ["專案", task.project],
      ["Agent", task.agent_provider_id || "-"],
      ["Task ID", task.id],
      ["ProjectVersion", task.project_version_id || "legacy／未綁定"],
      ["Immutable base", task.base_commit || task.observed_base_commit || "未綁定"],
      ["Phase", engineeringTaskPhaseLabel(task)],
      ["Execution health", (task.presentation && task.presentation.execution_health && task.presentation.execution_health.label) || "未知"],
      ["Runner connection", (task.presentation && task.presentation.runner_connection && task.presentation.runner_connection.label) || "未知"],
      ["Requester", (task.requester && task.requester.display_name) || "-"],
      ["Approver", (task.approver && task.approver.display_name) || "尚未決定"],
      ["Created", formatTimestamp(task.created_at)],
      ["Updated", formatTimestamp(task.updated_at)],
    ]);
    panel.append(grid);
    const finalResponse = task.final_response || {};
    const finalSection = node("section", null, "task-detail-section");
    finalSection.append(node("h4", "最終回覆"));
    if (finalResponse.withheld) {
      finalSection.append(node("p", "最終回覆因安全政策而隱藏。", "section-note"));
    } else if (finalResponse.available && typeof finalResponse.content === "string") {
      finalSection.append(node("pre", finalResponse.content, "approval-summary-pre"));
    } else {
      finalSection.append(node("p", "尚無最終回覆。", "section-note"));
    }
    panel.append(finalSection);
  }

  function detailListNode(rows) {
    const dl = node("dl", null, "detail-list");
    for (const [label, value] of rows) {
      const row = node("div");
      row.append(node("dt", label), node("dd", value == null || value === "" ? "-" : String(value)));
      dl.append(row);
    }
    return dl;
  }

  function renderEngineeringTimelineTab(task) {
    const panel = element("engineering-task-pane-timeline");
    panel.replaceChildren();
    detailHeading(panel, "進度時間軸");
    const events = Array.isArray(task.events) ? task.events : [];
    if (!events.length) {
      emptyDetailState(panel, "尚無可顯示的事件；缺少事件不會被推斷為失敗。");
      return;
    }
    const list = node("ol", null, "task-event-list");
    for (const eventItem of events) {
      const item = node("li", null, "task-event");
      item.append(node("strong", eventItem.summary || eventItem.type || "事件"));
      item.append(node("time", eventItem.occurred_at || eventItem.recorded_at || "時間未知"));
      if (eventItem.attempt_number != null) {
        item.append(node("span", ` Attempt ${eventItem.attempt_number}`, "task-metadata"));
      }
      list.append(item);
    }
    panel.append(list);
    if (events.length >= 100) {
      const loadMore = node("button", "載入更多事件", "button button-quiet");
      loadMore.type = "button";
      loadMore.addEventListener("click", () => loadMoreEngineeringTaskEvents(task));
      panel.append(loadMore);
    }
  }

  async function loadMoreEngineeringTaskEvents(task) {
    const events = Array.isArray(task.events) ? task.events : [];
    const lastId = events.reduce((max, item) => (
      Number.isSafeInteger(item.id) && item.id > max ? item.id : max
    ), 0);
    if (!lastId) return;
    try {
      const page = await productRead(
        `/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/events?after_id=${lastId}&limit=100`
      );
      if (state.engineeringDetailTask !== task || !Array.isArray(page)) return;
      task.events = [...events, ...page];
      renderEngineeringTimelineTab(task);
    } catch (error) {
      showAlert("載入更多事件失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  function renderEngineeringCommandsTab(task) {
    const panel = element("engineering-task-pane-commands");
    panel.replaceChildren();
    detailHeading(panel, "命令與經過遮罩的日誌");
    const commands = Array.isArray(task.commands) ? task.commands : [];
    if (!commands.length) {
      emptyDetailState(panel, "尚無命令記錄。任務尚未執行與伺服器無法確認是不同狀態。");
      return;
    }
    const list = node("div", null, "task-command-list");
    for (const command of commands) {
      const card = node("article", null, "task-command");
      card.append(node("span", window.WorkspaceUI.STATUS_LABEL[command.status] || command.status, `status-pill status-${command.status}`));
      card.append(node("strong", command.role || "Command"));
      const meta = [
        command.execution_location_label || "執行位置未公開",
        command.working_directory_label || "工作目錄未公開",
        command.started_at && `start ${command.started_at}`,
        command.finished_at && `end ${command.finished_at}`,
        command.exit_code != null && `exit ${command.exit_code}`,
        command.duration_seconds != null && `${command.duration_seconds}s`,
      ].filter(Boolean).join(" · ");
      if (meta) card.append(node("span", meta, "task-metadata"));
      card.append(node("code", command.display_command || "命令內容未提供"));
      const logInfo = command.log || {};
      if (logInfo.available === true) {
        const logButton = node("button", "查看經過遮罩的日誌", "button button-quiet");
        logButton.type = "button";
        logButton.addEventListener("click", () => loadEngineeringCommandLog(task, command, card, logButton));
        card.append(logButton);
      } else {
        card.append(node("p", "命令日誌目前不可用。", "task-metadata"));
      }
      list.append(card);
    }
    panel.append(list);
  }

  async function loadEngineeringCommandLog(task, command, card, button) {
    button.disabled = true;
    button.textContent = "載入中…";
    try {
      const response = await productRead(
        `/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/commands/${encodeURIComponent(command.id)}/log`
      );
      const existing = card.querySelector("pre.task-command-log");
      if (existing) existing.remove();
      const content = response && response.withheld === true
        ? "日誌包含高風險敏感內容，已整段隱藏。"
        : (response && response.available === true && typeof response.content === "string"
          ? response.content
          : "日誌回應未包含可安全顯示的內容。");
      card.append(node("pre", content, "approval-summary-pre task-command-log"));
    } catch (error) {
      card.append(node("p", "日誌載入失敗：" + (error instanceof Error ? error.message : "未知錯誤"), "field-error"));
    } finally {
      button.disabled = false;
      button.textContent = "重新載入經過遮罩的日誌";
    }
  }

  function renderEngineeringChangesTab(task) {
    const panel = element("engineering-task-pane-changes");
    panel.replaceChildren();
    detailHeading(panel, "變更與 Diff");
    const changes = task.changes || {};
    panel.append(node("p", changes.summary || "尚無變更摘要", "section-note"));
    if (changes.truncated) panel.append(node("p", "差異內容已截斷。", "task-metadata"));
    const diffState = node("div", null, "task-empty-state");
    diffState.id = "engineering-task-diff-state";
    diffState.textContent = changes.withheld === true
      ? "Diff 因安全政策而整段隱藏；瀏覽器不會發出內容請求。"
      : (changes.available === true ? "開啟此分頁後載入經後端遮罩的 diff。" : "伺服器未明確標示 diff 可用，內容維持隱藏。");
    panel.append(diffState);
  }

  async function loadEngineeringTaskDiff() {
    const task = state.engineeringDetailTask;
    if (!task) return;
    const changes = task.changes || {};
    const target = element("engineering-task-diff-state");
    if (!target || changes.withheld === true || changes.available !== true) return;
    if (target.dataset.loaded === "true") return;
    target.textContent = "正在載入經遮罩的 Diff…";
    try {
      const response = await productRead(`/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/diff`);
      if (state.engineeringDetailTask !== task) return;
      if (response && response.withheld === true) {
        target.textContent = "Diff 因安全政策而整段隱藏；瀏覽器不會顯示回應中的其他欄位。";
        return;
      }
      if (!response || response.available !== true || typeof response.patch !== "string") {
        target.textContent = "Diff 安全投影不完整，內容維持隱藏。";
        return;
      }
      const viewer = node("pre", response.patch || "（無 diff）", "approval-summary-pre");
      target.replaceChildren(viewer);
      target.dataset.loaded = "true";
    } catch (error) {
      target.textContent = "Diff 載入失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderEngineeringTestsTab(task) {
    const panel = element("engineering-task-pane-tests");
    panel.replaceChildren();
    detailHeading(panel, "測試與驗證");
    const tests = Array.isArray(task.tests) ? task.tests : [];
    const validations = Array.isArray(task.worker_validations) ? task.worker_validations : [];
    if (!tests.length && !validations.length) {
      emptyDetailState(panel, "尚無驗證結果；「未執行」不會被標示為測試失敗。");
      return;
    }
    if (tests.length) {
      const list = node("div", null, "task-test-list");
      for (const test of tests) {
        const card = node("article", null, "task-test");
        card.append(node("span", test.label || test.status, `status-pill status-${test.status}`));
        if (test.exit_code != null) card.append(node("span", ` exit ${test.exit_code}`, "task-metadata"));
        list.append(card);
      }
      panel.append(list);
    }
    if (validations.length) {
      const section = node("section", null, "task-detail-section");
      section.append(node("h4", "Worker validations"));
      const list = node("div", null, "task-test-list");
      for (const validation of validations) {
        const card = node("article", null, "task-test");
        card.append(node("span", validation.status, `status-pill status-${validation.status}`));
        card.append(node("strong", `Validation ${validation.id}`));
        const connection = validation.connection && validation.connection.code;
        const meta = [
          validation.attempt_number != null && `Attempt ${validation.attempt_number}`,
          validation.approval_id != null && `Approval #${validation.approval_id}`,
          connection && `Connection ${connection}`,
        ].filter(Boolean).join(" · ");
        if (meta) card.append(node("span", meta, "task-metadata"));
        list.append(card);
      }
      section.append(list);
      panel.append(section);
    }
  }

  function renderEngineeringArtifactsTab(task) {
    const panel = element("engineering-task-pane-artifacts");
    panel.replaceChildren();
    detailHeading(panel, "Artifacts");
    const artifacts = Array.isArray(task.artifacts) ? task.artifacts : [];
    if (!artifacts.length) {
      emptyDetailState(panel, "尚無 artifact metadata。清理後的記錄應保留歷史，並由後端標示 availability。");
      return;
    }
    const list = node("ul", null, "task-artifact-list");
    for (const artifact of artifacts) {
      const item = node("li", null, "task-artifact");
      item.append(node("strong", artifact.label || artifact.kind || artifact.artifact_key || "Artifact"));
      item.append(node("p", "Artifact 儲存位置不公開"));
      const meta = [
        artifact.availability,
        artifact.verification_status,
        artifact.redaction_status,
        artifact.sha256 && `sha256 ${artifact.sha256}`,
        artifact.size_bytes != null && `${artifact.size_bytes} bytes`,
      ].filter(Boolean).join(" · ");
      if (meta) item.append(node("span", meta, "task-metadata"));
      list.append(item);
    }
    panel.append(list);
  }

  function renderEngineeringRisksTab(task) {
    const panel = element("engineering-task-pane-risks");
    panel.replaceChildren();
    detailHeading(panel, "Risks / Warnings");
    const candidates = [
      ...(Array.isArray(task.warnings) ? task.warnings : []),
      ...((task.presentation && Array.isArray(task.presentation.warnings)) ? task.presentation.warnings : []),
    ];
    const warnings = Array.from(new Set(candidates.filter((warning) => typeof warning === "string" && warning.trim()).map((warning) => warning.trim())));
    const safetyFlags = [];
    if (task.final_response && task.final_response.withheld === true) safetyFlags.push("最終回覆已由伺服器安全政策隱藏。");
    if (task.changes && task.changes.withheld === true) safetyFlags.push("Diff 已由伺服器安全政策隱藏。");
    if (!warnings.length && !safetyFlags.length) {
      emptyDetailState(panel, "伺服器目前未回傳風險或警告；這不代表已完成全面安全審查。");
      return;
    }
    const list = node("ul", null, "task-risk-list");
    for (const warning of [...warnings, ...safetyFlags]) {
      const item = node("li", null, "ui-alert ui-alert-warning");
      item.append(node("span", "!"), node("div", warning));
      list.append(item);
    }
    panel.append(list);
  }

  function renderEngineeringApprovalsTab(task) {
    const panel = element("engineering-task-pane-approvals");
    panel.replaceChildren();
    detailHeading(panel, "核准歷史");
    const approvals = Array.isArray(task.approval_history) ? task.approval_history : [];
    if (!approvals.length) {
      emptyDetailState(panel, "尚無可顯示的核准歷史。");
      return;
    }
    const list = node("ol", null, "task-approval-list");
    for (const approval of approvals) {
      const item = node("li", null, "task-approval");
      item.append(node("strong", window.WorkspaceUI.KIND_LABEL[approval.kind] || approval.kind));
      item.append(node("p", window.WorkspaceUI.STATUS_LABEL[approval.status] || approval.status));
      const actor = (approval.approver && approval.approver.display_name)
        || (approval.requester && approval.requester.display_name) || "";
      const meta = [`#${approval.approval_id}`, actor, approval.decided_at || approval.created_at].filter(Boolean).join(" · ");
      item.append(node("span", meta, "task-approval-meta"));
      list.append(item);
    }
    panel.append(list);
  }

  function renderEngineeringTaskActions(task) {
    const retry = window.WorkspaceUI.engineeringAction(task, "retry");
    const discard = window.WorkspaceUI.engineeringAction(task, "discard");
    const promote = window.WorkspaceUI.engineeringAction(task, "promote");
    const cleanup = window.WorkspaceUI.engineeringAction(task, "cleanup");
    const patch = window.WorkspaceUI.engineeringAction(task, "download_patch");
    const validation = window.WorkspaceUI.engineeringAction(task, "request_worker_validation");

    const retryBtn = element("engineering-task-retry-btn");
    const discardBtn = element("engineering-task-discard-btn");
    const promoteBtn = element("engineering-task-promote-btn");
    const cleanupBtn = element("engineering-task-cleanup-btn");
    const patchBtn = element("engineering-task-download-patch-btn");
    const validationBtn = element("engineering-task-validation-btn");

    retryBtn.disabled = !(retry && retry.enabled === true);
    discardBtn.disabled = !(discard && discard.enabled === true);
    promoteBtn.disabled = !(promote && promote.enabled === true);
    cleanupBtn.disabled = !(cleanup && cleanup.enabled === true && cleanup.coding_run_id != null);
    patchBtn.disabled = !(patch && patch.enabled === true);
    validationBtn.disabled = !(validation && validation.enabled === true && validation.coding_run_id != null);

    retryBtn.title = (retry && retry.reason) || "伺服器未開啟此動作";
    discardBtn.title = (discard && discard.reason) || "伺服器未開啟此動作";
    promoteBtn.title = (promote && promote.reason) || "建立人工 promotion 核准請求";
    cleanupBtn.title = (cleanup && cleanup.reason) || "伺服器未開啟此動作";
    patchBtn.title = (patch && patch.reason) || "伺服器未回傳可用的去敏後的已收集 patch";
    validationBtn.title = (validation && validation.reason) || "伺服器未開啟此動作";

    retryBtn.onclick = () => engineeringTaskRetryAction(task);
    discardBtn.onclick = () => engineeringTaskDiscardAction(task);
    promoteBtn.onclick = () => engineeringTaskPromoteAction(task);
    cleanupBtn.onclick = () => engineeringTaskCleanupAction(task, cleanup);
    patchBtn.onclick = () => engineeringTaskDownloadPatchAction(task);
    validationBtn.onclick = () => engineeringTaskRequestValidationAction(task, validation);

    const reasons = [];
    if (retry && retry.reason) reasons.push(`Retry：${retry.reason}`);
    if (discard && discard.reason) reasons.push(`Discard：${discard.reason}`);
    if (promote && promote.reason) reasons.push(`Promote：${promote.reason}`);
    if (cleanup && cleanup.reason) reasons.push(`清理：${cleanup.reason}`);
    if (validation && validation.reason) reasons.push(`後續驗證：${validation.reason}`);
    element("engineering-task-action-note").textContent = reasons.length
      ? reasons.join("；")
      : "僅開啟伺服器 available_actions 明確允許的現有動作。";
  }

  async function engineeringTaskRetryAction(task) {
    try {
      await productMutation(`/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/retry-requests`, {});
      showAlert("已建立 retry 核准卡，請至「核准」分頁決定。");
      await openEngineeringTaskDetail(task.id);
    } catch (error) {
      showAlert("建立 retry 請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function engineeringTaskDiscardAction(task) {
    try {
      await productMutation(`/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/discard-requests`, {});
      showAlert("已建立 discard 核准卡，請至「核准」分頁決定。");
      await openEngineeringTaskDetail(task.id);
    } catch (error) {
      showAlert("建立 discard 請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function engineeringTaskPromoteAction(task) {
    try {
      await productMutation(`/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/promote-requests`, {});
      showAlert("已建立 promote 核准卡，請至「核准」分頁決定。");
      await openEngineeringTaskDetail(task.id);
    } catch (error) {
      showAlert("建立 promote 請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function engineeringTaskCleanupAction(task, cleanup) {
    if (!cleanup || cleanup.coding_run_id == null) return;
    try {
      await productMutation(`/api/v2/coding-runs/${encodeURIComponent(cleanup.coding_run_id)}/cleanup`, {});
      showAlert("已清理 worktree。");
      await openEngineeringTaskDetail(task.id);
    } catch (error) {
      showAlert("清理 worktree 失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function engineeringTaskDownloadPatchAction(task) {
    try {
      const { blob, redacted } = await authenticatedEngineeringPatchDownload(
        `/api/v2/engineering-tasks/${encodeURIComponent(task.id)}/patch`
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `engineering-task-${task.id}${redacted ? ".redacted" : ""}.patch`;
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      showAlert("下載修補檔失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function engineeringTaskRequestValidationAction(task, validation) {
    if (!validation || validation.coding_run_id == null) return;
    const command = window.prompt("後續 worker validation 指令：", "python3 -m pytest -q");
    if (command === null) return;
    const pinServer = window.prompt("目標機器名稱：", "");
    if (pinServer === null || !pinServer.trim()) return;
    try {
      await productMutation(
        `/api/v2/engineering-tasks/${encodeURIComponent(validation.engineering_task_id || task.id)}/worker-validation-requests`,
        { command, pin_server: pinServer.trim() }
      );
      showAlert("已建立後續驗證核准卡，請至「核准」分頁決定。");
      await openEngineeringTaskDetail(task.id);
    } catch (error) {
      showAlert("建立後續驗證請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  function renderEngineeringTaskDetail(task) {
    element("engineering-task-detail-title").textContent = `AI 工程任務 · ${window.WorkspaceUI.engineeringTaskShortId(task.id)}`;
    const statusBadge = element("engineering-task-detail-status");
    const statusCode = engineeringTaskStatusCode(task);
    statusBadge.textContent = engineeringTaskStatusLabel(task);
    statusBadge.className = `status-pill status-${statusCode}`;
    renderEngineeringOverviewTab(task);
    renderEngineeringTimelineTab(task);
    renderEngineeringCommandsTab(task);
    renderEngineeringChangesTab(task);
    renderEngineeringTestsTab(task);
    renderEngineeringArtifactsTab(task);
    renderEngineeringRisksTab(task);
    renderEngineeringApprovalsTab(task);
    renderEngineeringTaskActions(task);
    activateEngineeringTaskTab(state.engineeringDetailTab || "overview");
  }

  // ---- AI 工程精靈（建立 AI 工程任務）--------------------------------

  function openEngineeringWizard() {
    state.engineeringWizardOpen = true;
    state.engineeringWizardOpenSerial += 1;
    element("engineering-wizard-panel").hidden = false;
    element("engineering-wizard-status").textContent = "";
    loadEngineeringWizardProjects();
    loadEngineeringWizardCapabilitiesAndProviders();
    refreshEngineeringWizardRunnerStatus();
    updateEngineeringWizardPreview();
  }

  function closeEngineeringWizard() {
    state.engineeringWizardOpen = false;
    element("engineering-wizard-panel").hidden = true;
  }

  async function loadEngineeringWizardProjects() {
    const select = element("engineering-wizard-project");
    try {
      const projects = await productRead("/api/v2/legacy-projects");
      setSelectOptions(
        select,
        (Array.isArray(projects) ? projects : []).map((project) => ({ value: project.name, label: project.name })),
        "請選擇專案"
      );
    } catch (error) {
      showAlert("載入專案清單失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function loadEngineeringWizardCapabilitiesAndProviders() {
    try {
      const [capabilities, agents] = await Promise.all([
        productRead("/api/v2/engineering-tasks/capabilities"),
        productRead("/api/v2/coding-agents"),
      ]);
      state.engineeringWizardCapabilities = capabilities;
      const providers = (agents && Array.isArray(agents.providers) ? agents.providers : [])
        .filter((provider) => provider.operations && provider.operations.start_turn === true);
      state.engineeringWizardProviders = providers;
      const field = element("engineering-wizard-provider-field");
      field.hidden = providers.length < 2;
      setSelectOptions(
        element("engineering-wizard-provider"),
        providers.map((provider) => ({
          value: provider.provider_id,
          label: provider.display_name || provider.provider_id,
        })),
        "（自動選擇）"
      );
    } catch (error) {
      showAlert("載入 Coding Agent 能力失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function loadEngineeringWizardVersions(projectName) {
    const select = element("engineering-wizard-version");
    if (!projectName) {
      setSelectOptions(select, [], "請先選擇專案");
      return;
    }
    try {
      const versions = await productRead(`/api/v2/legacy-projects/${encodeURIComponent(projectName)}/versions`);
      state.engineeringWizardVersions = (Array.isArray(versions) ? versions : [])
        .filter((version) => version.promotion_state === "promoted");
      setSelectOptions(
        select,
        state.engineeringWizardVersions.map((version) => ({
          value: version.id,
          label: `${String(version.git_commit || "").slice(0, 12)}（${formatTimestamp(version.created_at)}）`,
        })),
        "請選擇 ProjectVersion"
      );
    } catch (error) {
      showAlert("載入 ProjectVersion 清單失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function refreshEngineeringWizardRunnerStatus() {
    const serial = state.engineeringWizardOpenSerial;
    const container = element("engineering-wizard-runner-state");
    container.className = "component-state state-loading";
    container.textContent = "正在確認 Coding Runner…";
    try {
      const status = await productRead("/api/v2/codex-runner/status");
      if (serial !== state.engineeringWizardOpenSerial) return;
      state.engineeringWizardRunnerStatus = status;
      state.engineeringWizardRunnerConnectionFailed = false;
    } catch (error) {
      if (serial !== state.engineeringWizardOpenSerial) return;
      state.engineeringWizardRunnerStatus = null;
      state.engineeringWizardRunnerConnectionFailed = true;
    }
    renderEngineeringWizardRunnerState();
  }

  function renderEngineeringWizardRunnerState() {
    const container = element("engineering-wizard-runner-state");
    const view = window.WorkspaceUI.codexRunnerStatusView(
      state.engineeringWizardRunnerStatus,
      state.engineeringWizardRunnerConnectionFailed
    );
    container.className = `component-state state-${view.variant}`;
    container.replaceChildren();
    const copy = node("div");
    copy.append(node("strong", view.title), node("p", view.note || ""));
    container.append(copy);
    const retry = node("button", "重新檢查", "button button-quiet");
    retry.type = "button";
    retry.addEventListener("click", refreshEngineeringWizardRunnerStatus);
    container.append(retry);
  }

  function readEngineeringWizardValues() {
    return {
      objective: element("engineering-wizard-objective").value.trim(),
      background: element("engineering-wizard-background").value.trim(),
      expectedChanges: element("engineering-wizard-expected-changes").value.trim(),
      nonGoals: element("engineering-wizard-non-goals").value.trim(),
      allowedPaths: element("engineering-wizard-allowed-paths").value.trim(),
      prohibitedPaths: element("engineering-wizard-prohibited-paths").value.trim(),
      prohibitedChanges: "",
      acceptanceCriteria: element("engineering-wizard-acceptance").value.trim(),
      runTests: element("engineering-wizard-tests").checked,
      runBuild: element("engineering-wizard-build").checked,
      autoFix: element("engineering-wizard-auto-fix").checked,
      workerValidation: Boolean(element("engineering-wizard-validation-target").value),
      validationTarget: element("engineering-wizard-validation-target").value,
    };
  }

  function engineeringWizardPathPolicyEnabled() {
    const capabilities = state.engineeringWizardCapabilities;
    return Boolean(capabilities && capabilities.contract_version === "engineering-task-v2");
  }

  function updateEngineeringWizardPreview() {
    const values = readEngineeringWizardValues();
    const instruction = window.WorkspaceUI.renderEngineeringTaskInstruction(values, {
      enforceFinalGitPaths: engineeringWizardPathPolicyEnabled(),
    });
    element("engineering-wizard-instruction-preview").textContent = instruction;
    const count = window.WorkspaceUI.engineeringCodePointLength(instruction);
    const limit = window.WorkspaceUI.ENGINEERING_INSTRUCTION_LIMIT;
    const counter = element("engineering-wizard-instruction-count");
    counter.textContent = `${count} / ${limit} 字元`;
    counter.classList.toggle("field-error", count > limit);
  }

  async function checkEngineeringWizardPathCoverage() {
    const container = element("engineering-wizard-coverage-result");
    const projectName = element("engineering-wizard-project").value;
    const versionId = element("engineering-wizard-version").value;
    if (!projectName || !versionId) {
      container.textContent = "請先選擇專案與 ProjectVersion 再檢查涵蓋範圍。";
      return;
    }
    const values = readEngineeringWizardValues();
    const validationError = window.WorkspaceUI.validateEngineeringPathPolicyInputs(values);
    if (validationError) {
      container.textContent = validationError.message;
      return;
    }
    container.textContent = "正在對照 pinned 版本的實際檔案…";
    try {
      const coverage = await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/engineering-task-path-policy-coverage`,
        {
          project_version_id: versionId,
          allowed_paths: window.WorkspaceUI.engineeringCanonicalPathItems(values.allowedPaths),
          prohibited_paths: window.WorkspaceUI.engineeringCanonicalPathItems(values.prohibitedPaths),
        }
      );
      const summary = node("div");
      summary.append(node(
        "p",
        coverage.truncated
          ? `已掃描前 ${coverage.tree_file_count} 個檔案（此版本檔案數超過預檢上限，計數可能為下界）。`
          : `已對照 ${coverage.tree_file_count} 個檔案。`,
        "muted"
      ));
      for (const [heading, rules] of [["Allowed paths", coverage.allowed || []], ["Prohibited paths", coverage.prohibited || []]]) {
        const section = node("div");
        section.append(node("strong", heading));
        if (!rules.length) {
          section.append(node("p", "（未設定）", "muted"));
        } else {
          const list = node("ul");
          for (const rule of rules) {
            const item = node("li");
            item.append(node("code", rule.scope), node("span", ` — 命中 ${rule.file_hits} 個檔案`));
            if (rule.matches_existing_directory) {
              item.append(node("span", `；exact 規則命中既有目錄名，是否想寫成「${rule.scope}/」？`, "field-error"));
            }
            if (rule.secret_protected) {
              item.append(node("span", "；此規則命中受保護的 secret 檔名樣式，final 結果一定會被拒絕。", "field-error"));
            }
            list.append(item);
          }
          section.append(list);
        }
        summary.append(section);
      }
      container.replaceChildren(summary);
    } catch (error) {
      container.textContent = "涵蓋範圍檢查失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  async function submitEngineeringWizard() {
    const status = element("engineering-wizard-status");
    const projectName = element("engineering-wizard-project").value;
    const versionId = element("engineering-wizard-version").value;
    const providerSelect = element("engineering-wizard-provider");
    const values = readEngineeringWizardValues();
    if (!projectName) {
      status.textContent = "請先選擇專案。";
      return;
    }
    if (!versionId) {
      status.textContent = "請先選擇 ProjectVersion（immutable base）。";
      return;
    }
    if (!values.objective) {
      status.textContent = "請填寫 Task objective。";
      return;
    }
    const pathPolicyEnabled = engineeringWizardPathPolicyEnabled();
    if (pathPolicyEnabled && !window.WorkspaceUI.engineeringBulletItems(values.allowedPaths).length) {
      status.textContent = "此 contract 已啟用 v2 path policy，allowed paths 為必填。";
      return;
    }
    const pathError = window.WorkspaceUI.validateEngineeringPathPolicyInputs(values);
    if (pathError) {
      status.textContent = pathError.message;
      return;
    }
    const instruction = window.WorkspaceUI.renderEngineeringTaskInstruction(values, { enforceFinalGitPaths: pathPolicyEnabled });
    if (window.WorkspaceUI.engineeringCodePointLength(instruction) > window.WorkspaceUI.ENGINEERING_INSTRUCTION_LIMIT) {
      status.textContent = `Generated instruction 超過 ${window.WorkspaceUI.ENGINEERING_INSTRUCTION_LIMIT} 字元，請縮短需求內容。`;
      return;
    }
    const body = {
      project_version_id: versionId,
      agent_provider_id: providerSelect.value || "codex",
      objective: values.objective,
      background: values.background || null,
      expected_changes: window.WorkspaceUI.engineeringBulletItems(values.expectedChanges),
      non_goals: window.WorkspaceUI.engineeringBulletItems(values.nonGoals),
      allowed_paths: window.WorkspaceUI.engineeringCanonicalPathItems(values.allowedPaths),
      prohibited_paths: window.WorkspaceUI.engineeringCanonicalPathItems(values.prohibitedPaths),
      prohibited_changes: [],
      acceptance_criteria: window.WorkspaceUI.engineeringBulletItems(values.acceptanceCriteria),
      validation: {
        tests_lint: values.runTests,
        build_smoke: values.runBuild,
        continue_fixing_failures: values.autoFix,
        worker_validation_target: values.validationTarget || null,
      },
      execution_permissions: {
        modify_project_files: true,
        install_dependencies: false,
        external_network: false,
      },
    };
    status.textContent = "送出中…";
    try {
      await productMutation(
        `/api/v2/legacy-projects/${encodeURIComponent(projectName)}/engineering-task-requests`,
        body
      );
      showAlert("已建立核准卡，請至「核准」分頁決定。");
      closeEngineeringWizard();
      await loadEngineeringTasks();
    } catch (error) {
      status.textContent = "送出失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  // ---- 基礎設施（DG-UI-UNIFICATION v1 U4）：thin `/api/v2/servers*` / -----
  // `/api/v2/server-configs*` / `/api/v2/inventory/*` / `/api/v2/codex-
  // runner/status` wrappers. Ported from `static/index.html`'s
  // `renderServerConfigTable()` (:6944-7012), the server add/edit modal
  // (:7014-7122), the inventory scan/candidates panel (:4015+), and
  // `renderCodingRunnerInfrastructureStatus()` (:6840-6909). Business logic
  // (health-line text, idle status label, codex runner branch order) lives
  // in `workspace-features.js`'s `WorkspaceUI.serverHealthLine`/
  // `idleSummaryStatusLabel`/`codexRunnerStatusView`; this file only builds
  // DOM nodes and wires events, matching the split used for jobs above.

  function splitCommaList(value) {
    return String(value || "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
  }

  async function loadInfraServers() {
    element("infra-workers-state").textContent = "正在載入機器清單…";
    try {
      const [configs, servers] = await Promise.all([
        productRead("/api/v2/server-configs"),
        productRead("/api/v2/servers"),
      ]);
      state.infraServerConfigs = Array.isArray(configs) ? configs : [];
      state.infraServers = {};
      for (const entry of Array.isArray(servers) ? servers : []) {
        state.infraServers[entry.name] = entry;
      }
      state.infraServerConfigsLoaded = true;
      element("infra-workers-state").textContent = `共 ${state.infraServerConfigs.length} 台機器。`;
      renderInfraWorkers();
    } catch (error) {
      element("infra-workers-state").textContent = "無法載入機器清單："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderInfraWorkers() {
    const body = element("infra-workers-tbody");
    body.replaceChildren();
    if (!state.infraServerConfigs.length) {
      const row = node("tr");
      const cell = node("td", "尚未設定任何機器", "empty-state");
      cell.colSpan = 11;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const cfg of state.infraServerConfigs) {
      body.append(buildInfraWorkerRow(cfg));
    }
  }

  function buildInfraWorkerRow(cfg) {
    const serverState = state.infraServers[cfg.name] || null;
    const row = node("tr", null, cfg.enabled ? "worker-row" : "worker-row row-disabled");
    const monitorLabel = !serverState || serverState.updated_at == null
      ? "尚未探測"
      : serverState.online === true
        ? "線上"
        : serverState.online === false
          ? "離線"
          : "狀態未知";
    const monitorCell = node("td");
    monitorCell.append(
      node("span", monitorLabel, "state-pill" + (serverState && serverState.online ? "" : " unavailable")),
      node("div", window.WorkspaceUI.serverHealthLine(serverState), "section-note")
    );

    const actionsCell = node("td", null, "button-row");
    const edit = node("button", "編輯", "button button-quiet");
    edit.type = "button";
    edit.addEventListener("click", () => openServerFormForEdit(cfg.name));
    actionsCell.append(edit);
    const testSsh = node("button", "測試 SSH", "button button-quiet");
    testSsh.type = "button";
    testSsh.addEventListener("click", () => testStoredServerSsh(cfg));
    actionsCell.append(testSsh);
    if (cfg.enabled) {
      const disable = node("button", "停用", "button button-quiet");
      disable.type = "button";
      disable.addEventListener("click", () => disableServerAction(cfg.name));
      actionsCell.append(disable);
    } else {
      const reenable = node("button", "重新啟用", "button button-quiet");
      reenable.type = "button";
      reenable.addEventListener("click", () => enableServerAction(cfg.name));
      actionsCell.append(reenable);
    }
    const del = node("button", "刪除", "button button-quiet");
    del.type = "button";
    del.addEventListener("click", () => deleteServerAction(cfg.name));
    actionsCell.append(del);

    row.append(
      node("td", cfg.name),
      node("td", cfg.host),
      node("td", cfg.user),
      node("td", String(cfg.port)),
      node("td", (cfg.tags || []).join(", ") || "-"),
      node("td", cfg.enabled ? "啟用中" : "已停用"),
      node("td", (cfg.project_roots || []).join(", ") || "-", "muted"),
      node("td", (cfg.dataset_roots || []).join(", ") || "-", "muted"),
      monitorCell,
      node("td", serverState && serverState.gpu_count != null ? String(serverState.gpu_count) : "-"),
      actionsCell
    );
    return row;
  }

  function resetServerFormFields() {
    for (const id of [
      "infra-server-name", "infra-server-host", "infra-server-user", "infra-server-key",
      "infra-server-tags", "infra-server-project-roots", "infra-server-dataset-roots",
      "infra-server-note",
    ]) {
      element(id).value = "";
    }
    element("infra-server-port").value = "22";
    element("infra-server-idle-gpu-util").value = "15";
    element("infra-server-idle-load").value = "2";
    element("infra-server-gpu").checked = false;
    element("infra-server-enabled").checked = true;
    element("infra-server-name").disabled = false;
    element("infra-server-test-ssh-result").textContent = "";
    element("infra-server-form-status").textContent = "填寫完成後可先測試 SSH，再送出請求。";
  }

  function openServerFormForAdd() {
    state.infraServerFormMode = "add";
    state.infraServerFormEditingName = null;
    element("infra-server-form-title").textContent = "新增伺服器";
    resetServerFormFields();
    element("infra-server-submit-btn").textContent = "送出新增請求";
    element("infra-server-form").hidden = false;
  }

  function openServerFormForEdit(name) {
    const cfg = state.infraServerConfigs.find((entry) => entry.name === name);
    if (!cfg) return;
    state.infraServerFormMode = "edit";
    state.infraServerFormEditingName = name;
    element("infra-server-form-title").textContent = `編輯伺服器：${name}`;
    resetServerFormFields();
    element("infra-server-name").value = cfg.name;
    element("infra-server-name").disabled = true; // 不支援改名
    element("infra-server-host").value = cfg.host;
    element("infra-server-port").value = String(cfg.port);
    element("infra-server-user").value = cfg.user;
    element("infra-server-key").value = cfg.key;
    element("infra-server-tags").value = (cfg.tags || []).join(",");
    element("infra-server-project-roots").value = (cfg.project_roots || []).join(",");
    element("infra-server-dataset-roots").value = (cfg.dataset_roots || []).join(",");
    element("infra-server-gpu").checked = Boolean(cfg.gpu);
    element("infra-server-enabled").checked = Boolean(cfg.enabled);
    element("infra-server-idle-gpu-util").value = String(cfg.idle_gpu_util != null ? cfg.idle_gpu_util : 15);
    element("infra-server-idle-load").value = String(cfg.idle_load != null ? cfg.idle_load : 2);
    element("infra-server-note").value = cfg.note || "";
    element("infra-server-submit-btn").textContent = "送出更新請求";
    element("infra-server-form").hidden = false;
  }

  function closeServerForm() {
    element("infra-server-form").hidden = true;
  }

  function readServerFormPayload() {
    return {
      name: element("infra-server-name").value.trim(),
      host: element("infra-server-host").value.trim(),
      port: parseInt(element("infra-server-port").value.trim() || "22", 10) || 22,
      user: element("infra-server-user").value.trim(),
      key: element("infra-server-key").value.trim(),
      gpu: element("infra-server-gpu").checked,
      idle_gpu_util: parseFloat(element("infra-server-idle-gpu-util").value) || 0,
      idle_load: parseFloat(element("infra-server-idle-load").value) || 0,
      tags: splitCommaList(element("infra-server-tags").value),
      project_roots: splitCommaList(element("infra-server-project-roots").value),
      dataset_roots: splitCommaList(element("infra-server-dataset-roots").value),
      enabled: element("infra-server-enabled").checked,
      note: element("infra-server-note").value.trim() || null,
    };
  }

  async function testServerFormSsh() {
    const payload = readServerFormPayload();
    const result = element("infra-server-test-ssh-result");
    if (!payload.name || !payload.host || !payload.user || !payload.key) {
      result.textContent = "name/host/user/key 為必填才能測試";
      return;
    }
    result.textContent = "測試中…";
    try {
      const data = await productMutation("/api/v2/server-configs/test-ssh", payload);
      result.textContent = data.ok
        ? "SSH 測試成功：" + JSON.stringify(data.results)
        : "SSH 測試失敗：" + JSON.stringify(data.errors || data.warnings);
    } catch (error) {
      result.textContent = "測試失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  async function testStoredServerSsh(cfg) {
    try {
      const data = await productMutation("/api/v2/server-configs/test-ssh", cfg);
      showAlert(data.ok ? `SSH 測試成功：${cfg.name}` : `SSH 測試失敗：${cfg.name}`);
    } catch (error) {
      showAlert("測試失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function submitServerForm() {
    const payload = readServerFormPayload();
    const status = element("infra-server-form-status");
    if (!payload.name || !payload.host || !payload.user || !payload.key) {
      status.textContent = "name/host/user/key 為必填";
      return;
    }
    status.textContent = "送出中…";
    try {
      if (state.infraServerFormMode === "add") {
        await productMutation("/api/v2/server-configs/add-requests", payload);
        showAlert("已建立新增伺服器請求，請到「核准」核准");
      } else {
        const updates = Object.assign({}, payload);
        delete updates.name;
        await productMutation("/api/v2/server-configs/update-requests", {
          name: state.infraServerFormEditingName,
          updates,
        });
        showAlert("已建立更新伺服器請求，請到「核准」核准");
      }
      closeServerForm();
      loadInfraServers();
    } catch (error) {
      status.textContent = "建立請求失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  async function enableServerAction(name) {
    try {
      await productMutation("/api/v2/server-configs/update-requests", {
        name,
        updates: { enabled: true },
      });
      showAlert("已建立重新啟用請求，請到「核准」核准");
      loadInfraServers();
    } catch (error) {
      showAlert("建立請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function disableServerAction(name) {
    if (!window.confirm(`確定要建立停用「${name}」的請求嗎？核准當下若該機器有執行中任務會被拒絕。`)) return;
    try {
      await productMutation("/api/v2/server-configs/disable-requests", { name });
      showAlert("已建立停用請求，請到「核准」核准");
      loadInfraServers();
    } catch (error) {
      showAlert("建立請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function deleteServerAction(name) {
    if (!window.confirm(`確定要建立刪除「${name}」的請求嗎？`)) return;
    try {
      await productMutation("/api/v2/server-configs/delete-requests", { name });
      showAlert("已建立刪除請求，請到「核准」核准");
      loadInfraServers();
    } catch (error) {
      showAlert("建立請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function loadInfraIdleSummary() {
    const hours = parseInt(element("infra-idle-hours").value, 10) || 24;
    element("infra-idle-state").textContent = "正在載入閒置摘要…";
    try {
      const data = await productRead(`/api/v2/servers/idle-summary?hours=${hours}`);
      state.infraIdleSummary = data;
      element("infra-idle-state").textContent = `${data.window_hours} 小時內共 ${data.servers.length} 台機器。`;
      renderInfraIdleSummary();
    } catch (error) {
      element("infra-idle-state").textContent = "無法載入閒置摘要："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderInfraIdleSummary() {
    const body = element("infra-idle-tbody");
    body.replaceChildren();
    const servers = (state.infraIdleSummary && state.infraIdleSummary.servers) || [];
    if (!servers.length) {
      const row = node("tr");
      const cell = node("td", "沒有已設定的機器", "empty-state");
      cell.colSpan = 9;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const summary of servers) {
      const row = node("tr");
      row.append(
        node("td", summary.server_name),
        node("td", window.WorkspaceUI.idleSummaryStatusLabel(summary.status)),
        node("td", summary.sample_count == null ? "-" : String(summary.sample_count)),
        node("td", summary.online_ratio == null ? "-" : `${Math.round(summary.online_ratio * 100)}%`),
        node("td", summary.load1_p50 == null ? "-" : String(summary.load1_p50)),
        node("td", summary.load1_p95 == null ? "-" : String(summary.load1_p95)),
        node("td", summary.gpu_util_p50 == null ? "-" : String(summary.gpu_util_p50)),
        node("td", summary.gpu_util_p95 == null ? "-" : String(summary.gpu_util_p95)),
        node("td", summary.continuous_idle_seconds == null ? "-" : `${summary.continuous_idle_seconds} 秒`)
      );
      body.append(row);
    }
  }

  async function loadInfraCandidates() {
    element("infra-candidate-state").textContent = "正在載入候選清單…";
    const params = new URLSearchParams();
    const server = element("infra-candidate-filter-server").value.trim();
    const status = element("infra-candidate-filter-status").value;
    const query = element("infra-candidate-filter-query").value.trim();
    if (server) params.set("server", server);
    if (status) params.set("status", status);
    if (query) params.set("q", query);
    const suffix = params.toString();
    try {
      const candidates = await productRead(
        `/api/v2/inventory/candidates${suffix ? `?${suffix}` : ""}`
      );
      state.infraCandidates = Array.isArray(candidates) ? candidates : [];
      state.infraCandidatesLoaded = true;
      element("infra-candidate-state").textContent = `共 ${state.infraCandidates.length} 筆候選。`;
      renderInfraCandidates();
    } catch (error) {
      element("infra-candidate-state").textContent = "無法載入候選清單："
        + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function renderInfraCandidates() {
    const container = element("infra-candidate-list");
    container.replaceChildren();
    if (!state.infraCandidates.length) {
      container.append(emptyState("沒有候選專案", "掃描機器後，候選會出現在這裡。"));
      return;
    }
    for (const candidate of state.infraCandidates) {
      container.append(buildCandidateCard(candidate));
    }
  }

  function buildCandidateCard(candidate) {
    const card = node("article", null, "item-card");
    const header = node("div", null, "item-card-header");
    header.append(node("h3", candidate.name_guess || candidate.path));
    header.append(node("span", candidate.status, "honesty-label"));
    card.append(header);
    const meta = node("div", null, "item-meta");
    meta.append(
      node("span", `機器 ${candidate.server}`),
      node("span", candidate.path),
      node("span", `信心度 ${candidate.confidence == null ? "未知" : candidate.confidence}`)
    );
    card.append(meta);
    if (Array.isArray(candidate.markers) && candidate.markers.length) {
      card.append(node("p", `標記：${candidate.markers.join(", ")}`, "section-note"));
    }
    if (candidate.embedded_data_summary) {
      card.append(node("p", `內嵌資料：${candidate.embedded_data_summary}`, "section-note"));
    }
    if (candidate.readme_excerpt) {
      const excerpt = candidate.readme_excerpt.length > 240
        ? `${candidate.readme_excerpt.slice(0, 240)}…`
        : candidate.readme_excerpt;
      card.append(node("pre", excerpt, "approval-summary-pre"));
    }
    if (candidate.status === "pending") {
      const actions = node("div", null, "button-row");
      const importBtn = node("button", "匯入", "button button-primary");
      importBtn.type = "button";
      importBtn.addEventListener("click", () => openImportForm(candidate));
      actions.append(importBtn);
      const ignoreBtn = node("button", "忽略", "button button-quiet");
      ignoreBtn.type = "button";
      ignoreBtn.addEventListener("click", () => ignoreCandidateAction(candidate.id));
      actions.append(ignoreBtn);
      card.append(actions);
    }
    return card;
  }

  async function scanInventoryAction() {
    const server = element("infra-scan-server").value.trim();
    const status = element("infra-scan-status");
    if (!server) {
      status.textContent = "請先填寫機器名稱或 all。";
      return;
    }
    status.textContent = "送出中…";
    try {
      const result = await productMutation("/api/v2/inventory/scan-requests", { server });
      const count = Array.isArray(result.approvals) ? result.approvals.length : 1;
      showAlert(`已建立 ${count} 張掃描核准卡，請到「核准」核准`);
      status.textContent = "";
    } catch (error) {
      status.textContent = "建立掃描請求失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function openManualAddForm() {
    element("infra-manual-server").value = "";
    element("infra-manual-path").value = "";
    element("infra-manual-name").value = "";
    element("infra-manual-add-status").textContent = "";
    element("infra-manual-add-form").hidden = false;
  }

  function closeManualAddForm() {
    element("infra-manual-add-form").hidden = true;
  }

  async function submitManualAddAction() {
    const server = element("infra-manual-server").value.trim();
    const path = element("infra-manual-path").value.trim();
    const name = element("infra-manual-name").value.trim() || null;
    const status = element("infra-manual-add-status");
    if (!server || !path) {
      status.textContent = "機器與路徑為必填";
      return;
    }
    status.textContent = "送出中…";
    try {
      await productMutation("/api/v2/inventory/candidates", { server, path, name });
      showAlert("已新增候選（不需核准；匯入仍需核准）");
      closeManualAddForm();
      loadInfraCandidates();
    } catch (error) {
      status.textContent = "新增候選失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  function openImportForm(candidate) {
    state.infraImportCandidateId = candidate.id;
    element("infra-import-form-title").textContent = `匯入候選：${candidate.name_guess || candidate.path}`;
    element("infra-import-name").value = "";
    element("infra-import-default-command").value = "";
    element("infra-import-dataset-mode").value = "";
    element("infra-import-dataset-name").value = "";
    element("infra-import-dataset-version").value = "";
    element("infra-import-require-tag").value = "";
    element("infra-import-setup-cmd").value = "";
    element("infra-import-summary").value = "";
    element("infra-import-status").textContent = "";
    element("infra-import-form").hidden = false;
  }

  function closeImportForm() {
    element("infra-import-form").hidden = true;
    state.infraImportCandidateId = null;
  }

  async function submitImportAction() {
    if (!state.infraImportCandidateId) return;
    const status = element("infra-import-status");
    status.textContent = "送出中…";
    const body = {
      name: element("infra-import-name").value.trim() || null,
      default_command: element("infra-import-default-command").value.trim() || null,
      dataset_mode: element("infra-import-dataset-mode").value || null,
      dataset_name: element("infra-import-dataset-name").value.trim() || null,
      dataset_version: element("infra-import-dataset-version").value.trim() || null,
      require_tag: element("infra-import-require-tag").value.trim() || null,
      setup_cmd: element("infra-import-setup-cmd").value.trim() || null,
      summary: element("infra-import-summary").value.trim() || null,
    };
    try {
      await productMutation(
        `/api/v2/inventory/candidates/${encodeURIComponent(state.infraImportCandidateId)}/import-requests`,
        body
      );
      showAlert("已建立匯入請求，請到「核准」核准");
      closeImportForm();
      loadInfraCandidates();
    } catch (error) {
      status.textContent = "建立匯入請求失敗：" + (error instanceof Error ? error.message : "未知錯誤");
    }
  }

  async function ignoreCandidateAction(candidateId) {
    try {
      await productMutation(
        `/api/v2/inventory/candidates/${encodeURIComponent(candidateId)}/ignore-requests`,
        {}
      );
      showAlert("已建立忽略請求，請到「核准」核准");
      loadInfraCandidates();
    } catch (error) {
      showAlert("建立忽略請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function ignoreNestedCandidatesAction() {
    try {
      await productMutation("/api/v2/inventory/candidates/ignore-nested-requests", {});
      showAlert("已建立批次忽略巢狀候選請求，請到「核准」核准");
      loadInfraCandidates();
    } catch (error) {
      showAlert("建立請求失敗：" + (error instanceof Error ? error.message : "未知錯誤"));
    }
  }

  async function loadInfraCodexRunnerStatus() {
    element("infra-codex-status").textContent = "正在載入 Coding Runner 狀態…";
    try {
      state.infraCodexRunnerStatus = await productRead("/api/v2/codex-runner/status");
      state.infraCodexRunnerConnectionFailed = false;
    } catch (_error) {
      state.infraCodexRunnerStatus = null;
      state.infraCodexRunnerConnectionFailed = true;
    }
    renderInfraCodexRunnerStatus();
  }

  function renderInfraCodexRunnerStatus() {
    const container = element("infra-codex-status");
    container.replaceChildren();
    const view = window.WorkspaceUI.codexRunnerStatusView(
      state.infraCodexRunnerStatus,
      state.infraCodexRunnerConnectionFailed
    );
    container.append(node("strong", view.title));
    if (view.note) container.append(node("p", view.note, "section-note"));
    if (view.summary) {
      const dl = node("dl", null, "detail-list");
      for (const [label, value] of view.summary) appendDetail(dl, label, value);
      container.append(dl);
    }
  }

  function renderApprovals() {
    const container = element("approval-list");
    container.replaceChildren();
    if (!state.workspace.pending_approvals.length) {
      container.append(emptyState("沒有可見的 pending approval", "跨 Project 或不可解析的資料不會出現在這裡。"));
      return;
    }
    for (const approval of state.workspace.pending_approvals) {
      const card = node("article", null, "item-card");
      const header = node("div", null, "item-card-header");
      const kindLabel = window.WorkspaceUI.KIND_LABEL[approval.kind] || approval.kind;
      header.append(node("h3", `#${approval.id} · ${kindLabel}`));
      const disposition = approval.can_decide ? "可決定" : "僅可查看";
      header.append(node("span", disposition, "honesty-label"));
      card.append(header);
      const meta = node("div", null, "item-meta");
      meta.append(
        node("span", window.WorkspaceUI.approvalCategoryLabel(approval.kind)),
        node("span", approval.project_id ? `專案 · ${approval.project_id}` : "平台範圍"),
        node("span", formatTimestamp(approval.created_at)),
        node("span", approval.requester_is_self ? "我提出的" : "他人提出")
      );
      card.append(meta);
      //: DG-UI-UNIFICATION v1 U1: every `VALID_APPROVAL_KINDS` member is now
      //: inspectable and decidable through the same detail-review panel —
      //: there is no "尚未遷移的相容流程" dead end left in the Workspace.
      const actions = node("div", null, "button-row");
      const reviewLabel = COMPATIBILITY_APPROVAL_KINDS.has(approval.kind)
        ? "檢視與決定"
        : "檢視完整 immutable contract";
      const review = node("button", reviewLabel, "button button-quiet");
      review.type = "button";
      review.addEventListener("click", () => loadApprovalDetail(approval.id, review));
      actions.append(review);
      card.append(actions);
      container.append(card);
    }
    renderApprovalDetail();
  }

  function renderApprovalDetail() {
    const detail = state.approvalDetail;
    const panel = element("approval-review-panel");
    const meta = element("approval-review-meta");
    const summary = element("approval-review-summary");
    const payload = element("approval-review-payload");
    const confirmRow = element("approval-review-confirm-row");
    const confirm = element("approval-review-confirm");
    const oneTimeSecretNote = element("approval-review-one-time-secret-note");
    const actions = element("approval-review-actions");
    const approve = element("approval-review-approve");
    const reject = element("approval-review-reject");
    meta.replaceChildren();
    summary.replaceChildren();
    payload.textContent = "";
    confirm.checked = false;
    confirm.disabled = false;
    state.approvalDetailReviewed = false;
    state.approvalDetailOneTimeSecret = false;
    approve.disabled = true;
    approve.dataset.idempotencyKey = "";
    reject.dataset.idempotencyKey = "";
    oneTimeSecretNote.hidden = true;
    oneTimeSecretNote.textContent = "";
    if (!detail) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const kindLabel = window.WorkspaceUI.KIND_LABEL[detail.kind] || detail.kind;
    element("approval-review-title").textContent = `#${detail.id} · ${kindLabel}`;
    const compatibilitySnapshot = COMPATIBILITY_APPROVAL_KINDS.has(detail.kind)
      && detail.review_mode === "compatibility_snapshot"
      && detail.payload_verified === false
      && /^[0-9a-f]{64}$/.test(detail.payload_digest || "");
    element("approval-review-verification").textContent = detail.payload_verified
      ? "摘要碼已驗證"
      : compatibilitySnapshot ? "相容快照（非 immutable contract）" : "未驗證";
    appendDetail(meta, "契約版本", detail.payload_contract_version || "legacy 未鎖定版本");
    appendDetail(meta, "內容摘要碼", detail.payload_digest);
    appendDetail(meta, "請求者", detail.requester_actor_id);
    appendDetail(meta, "狀態", detail.status);
    //: DG-UI-UNIFICATION v1 U1: Chinese per-kind summary first (from
    //: `workspace-features.js`), the raw payload stays available below in a
    //: collapsed `<details>` —审核证据不丟，但預設不用整包 JSON 開場。
    window.WorkspaceUI.renderApprovalSummary(summary, detail);
    payload.textContent = JSON.stringify(detail.payload, null, 2);
    const canDecideReviewed = REVIEWED_APPROVAL_KINDS.has(detail.kind)
      && detail.payload_verified === true;
    const canDecideCompatibility = compatibilitySnapshot;
    const canDecide = detail.status === "pending"
      && detail.can_decide
      && (canDecideReviewed || canDecideCompatibility);
    const isOneTimeSecret = ONE_TIME_SECRET_APPROVAL_KINDS.has(detail.kind);
    state.approvalDetailOneTimeSecret = isOneTimeSecret;
    const approveLabels = {
      project_bootstrap_v2: "核准 Project Bootstrap",
      environment_change_v2: "核准 Environment revision",
      run_template_change_v2: "核准 Run Template revision",
      project_defaults_change_v2: "核准 Project Defaults revision",
      dataset_alias_change_v2: "核准 Dataset Alias revision",
      dataset_publish_v2: "核准 Dataset Publish",
      dataset_share_offer_v2: "核准 Dataset Share Offer",
      dataset_share_accept_v2: "核准 Dataset Share Accept",
      dataset_grant_revoke_v2: "核准 Dataset Grant 變更",
      execution_plan_v2: "核准 ExecutionPlan v2",
      stop: "核准 Stop request",
      enqueue: "核准並建立 Job",
    };
    const experimentRunCount = detail.kind === "experiment_create_v2"
      && detail.payload
      && typeof detail.payload.run_count === "number"
      ? detail.payload.run_count
      : null;
    approve.textContent = experimentRunCount !== null
      ? `核准 Experiment（${experimentRunCount} runs）`
      : approveLabels[detail.kind] || (compatibilitySnapshot ? `核准「${kindLabel}」` : "核准 immutable contract");
    element("approval-review-confirm-text").textContent = compatibilitySnapshot
      ? "我已檢視上方中文摘要與原始內容、snapshot digest；送出時伺服器必須重新核對同一份內容。"
      : "我已檢視上方完整 payload 與 digest，確認核准的是這份 immutable contract。";
    confirmRow.hidden = !canDecide;
    actions.hidden = !canDecide;
    if (canDecide && isOneTimeSecret) {
      //: `ONE_TIME_SECRET_APPROVAL_KINDS`（service_token_issue/node_enroll/
      //: node_rotate）：approve 停用＋說明，reject 維持可用（比照 legacy
      //: 停用按鈕語意）。
      confirm.disabled = true;
      approve.disabled = true;
      oneTimeSecretNote.hidden = false;
      oneTimeSecretNote.textContent = (
        "原因：此類核准會發放一次性秘密，v2 介面尚無安全顯示通道；"
        + "請改用能安全接收一次性 response 的管理 client 核准，或在此拒絕。"
      );
    }
  }

  async function loadApprovalDetail(approvalId, button) {
    button.disabled = true;
    state.approvalDetail = null;
    renderApprovalDetail();
    clearAlert();
    try {
      const detail = await productRead(`/api/v2/approvals/${approvalId}`);
      const verifiedContract = detail && detail.payload_verified === true;
      const compatibilitySnapshot = detail
        && COMPATIBILITY_APPROVAL_KINDS.has(detail.kind)
        && detail.review_mode === "compatibility_snapshot"
        && detail.payload_verified === false
        && /^[0-9a-f]{64}$/.test(detail.payload_digest || "");
      if (!detail || detail.id !== approvalId || (!verifiedContract && !compatibilitySnapshot)) {
        throw new Error("Approval detail verification failed");
      }
      state.approvalDetail = detail;
      renderApprovalDetail();
      element("approval-review-panel").scrollIntoView({ block: "start" });
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "無法載入 approval detail");
    } finally {
      button.disabled = false;
    }
  }

  async function decideReviewedApproval(decision, button) {
    const detail = state.approvalDetail;
    const verifiedContract = detail
      && REVIEWED_APPROVAL_KINDS.has(detail.kind)
      && detail.payload_verified === true;
    const compatibilitySnapshot = detail
      && COMPATIBILITY_APPROVAL_KINDS.has(detail.kind)
      && detail.review_mode === "compatibility_snapshot"
      && detail.payload_verified === false
      && /^[0-9a-f]{64}$/.test(detail.payload_digest || "");
    if (!detail || (!verifiedContract && !compatibilitySnapshot)) return;
    if (decision === "approve" && !state.approvalDetailReviewed) return;
    if (decision === "approve" && ONE_TIME_SECRET_APPROVAL_KINDS.has(detail.kind)) return;
    button.disabled = true;
    clearAlert();
    try {
      const idempotencyKey = button.dataset.idempotencyKey || randomUUID();
      button.dataset.idempotencyKey = idempotencyKey;
      const result = await productMutation(
        `/api/v2/approvals/${detail.id}/decisions`,
        { decision, note: "Reviewed in Product v2 approval detail" },
        {
          idempotencyKey,
          payloadDigest: compatibilitySnapshot ? detail.payload_digest : null,
        }
      );
      await initialize();
      const destination = ["dataset_alias_change_v2", "dataset_publish_v2"].includes(detail.kind) || DATASET_SHARING_APPROVAL_KINDS.has(detail.kind)
        ? "datasets"
        : ["execution_plan_v2", "experiment_create_v2", "stop", "enqueue"].includes(detail.kind) ? "runs" : "projects";
      activateSection(destination);
      if (result.project_id && !["dataset_alias_change_v2", "dataset_publish_v2"].includes(detail.kind) && !DATASET_SHARING_APPROVAL_KINDS.has(detail.kind) && !["execution_plan_v2", "experiment_create_v2", "stop"].includes(detail.kind)) {
        await loadProjectWorkspace(result.project_id);
      }
      if (decision === "approve" && detail.kind === "project_bootstrap_v2") {
        showAlert(`Project ${result.project_id} 已原子建立。`);
      } else if (decision === "approve" && detail.kind === "run_template_change_v2") {
        showAlert(`Run Template revision ${result.revision} 已建立。`);
      } else if (decision === "approve" && detail.kind === "project_defaults_change_v2") {
        showAlert(`Project Defaults revision ${result.revision} 已建立。`);
      } else if (decision === "approve" && detail.kind === "dataset_publish_v2") {
        if (result.state === "published") {
          showAlert(`Dataset Asset ${result.asset_id} 已發佈；snapshot ${result.snapshot_id}。`);
        } else {
          showAlert(`Dataset publish 已核准並保留 ${result.state || "building"} 狀態，可用同一 approval 安全重試。`);
        }
      } else if (decision === "approve" && detail.kind === "dataset_alias_change_v2") {
        const aliasName = detail.payload && detail.payload.target_revision
          ? detail.payload.target_revision.alias_name
          : "unknown";
        showAlert(`Dataset alias ${aliasName} revision ${result.revision} 已建立。`);
      } else if (decision === "approve" && detail.kind === "dataset_share_offer_v2") {
        showAlert(`Dataset Share Offer ${result.offer_id} 已建立。`);
      } else if (decision === "approve" && detail.kind === "dataset_share_accept_v2") {
        showAlert(`Dataset Share Offer ${result.offer_id} 已接受；已建立 ${(result.grant_ids || []).length} 個 grant。`);
      } else if (decision === "approve" && detail.kind === "dataset_grant_revoke_v2") {
        showAlert(`Dataset Grant ${result.grant_id} 已完成 ${result.operation || "變更"}。`);
      } else if (decision === "approve" && detail.kind === "execution_plan_v2") {
        showAlert(`ExecutionPlan ${result.execution_plan_id} 已核准；Job ${result.job_id} 仍由 canonical scheduler 管理。`);
      } else if (decision === "approve" && detail.kind === "experiment_create_v2") {
        showAlert(`Experiment ${result.experiment_id} 已核准；${(result.job_ids || []).length} 個 Job 已進入排程，仍由 canonical scheduler 管理。`);
      } else if (decision === "approve" && detail.kind === "stop") {
        showAlert(`Stop approval #${detail.id} 已核准；Run 顯示 stopping，尚未宣稱 terminal。`);
        if (result.plan_id) await loadRunDetail(result.plan_id);
      } else if (decision === "approve" && detail.kind === "enqueue") {
        showAlert(`Approval #${detail.id} 已核准；Job ${result.job_id || "已建立"} 已進入排程。`);
      } else if (decision === "approve" && COMPATIBILITY_APPROVAL_KINDS.has(detail.kind)) {
        //: DG-UI-UNIFICATION v1 U1: every other legacy kind decided through
        //: the generic compatibility decision branch — same
        //: `approvals_module.approve()` engine as legacy `POST /approve/{id}`.
        const kindLabel = window.WorkspaceUI.KIND_LABEL[detail.kind] || detail.kind;
        showAlert(`Approval #${detail.id}（${kindLabel}）已核准。`);
      } else if (decision === "approve") {
        showAlert(`Environment revision ${result.environment_revision_id} 已建立。`);
      } else {
        showAlert(`Approval #${detail.id} 已拒絕。`);
      }
    } catch (error) {
      button.disabled = false;
      showAlert(error instanceof Error ? error.message : "無法決定 approval");
    }
  }

  //: DG-UI-UNIFICATION v1 U7 (docs/DECISIONS.md 2026-08-25): 助手（chat
  //: assistant）— ported from `static/index.html`'s `connectChatSocket()`/
  //: `handleChatIncoming()`/`sendChatMessage()` (:6540-6810). Same `/ws`
  //: endpoint, same auth-frame semantics (`{"type":"auth","token":...}` sent
  //: only when a legacy token is in play -- session-cookie identity needs no
  //: first frame, see `app/main.py` `_ws_authenticate()`), same exponential
  //: backoff capped at 15s. Deliberate simplification vs. legacy: incoming
  //: `approval_card` messages render through the U1
  //: `window.WorkspaceUI.renderApprovalSummary()` Chinese summary plus a
  //: "前往核准區" button that jumps to the 核准 section and opens that
  //: approval's detail -- there is no in-chat approve/reject here (that
  //: duplicated the legacy `/approve|/reject` calls this generic engine
  //: also uses); every decision now goes through the single U1 decision
  //: channel. `auto_approved` cards (K.2/K.3 web-direct-execute / rule
  //: auto-approval) collapse to a one-line status instead of a full card --
  //: the approval is already decided, there is nothing left to review.

  function chatSetStatus(connected, text) {
    const pill = element("assistant-status");
    if (!pill) return;
    pill.classList.toggle("unavailable", !connected);
    pill.textContent = text;
  }

  function chatAppendMessage(role, text) {
    const box = element("assistant-messages");
    if (!box) return;
    const row = node("div", null, `assistant-msg assistant-msg-${role}`);
    row.append(node("p", text));
    box.append(row);
    box.scrollTop = box.scrollHeight;
  }

  //: Renders one incoming pending `approval_card` as an assistant message:
  //: title line (KIND_LABEL) + the U1 Chinese summary nodes (never the raw
  //: payload -- that stays behind the collapsed `<details>` in the 核准
  //: section's own detail panel) + a button that activates that section and
  //: loads the same approval's detail via the existing `loadApprovalDetail`.
  function chatAppendApprovalCard(approval) {
    const box = element("assistant-messages");
    if (!box || !approval) return;
    const kindLabel = window.WorkspaceUI.KIND_LABEL[approval.kind] || approval.kind;
    const row = node("div", null, "assistant-msg assistant-msg-assistant");
    row.append(node("strong", `待核准：#${approval.id} · ${kindLabel}`));
    const summary = node("div", null, "approval-summary-block");
    window.WorkspaceUI.renderApprovalSummary(summary, approval);
    row.append(summary);
    const goToApprovals = node("button", "前往核准區", "button button-quiet");
    goToApprovals.type = "button";
    goToApprovals.addEventListener("click", () => {
      activateSection("approvals");
      loadApprovalDetail(approval.id, goToApprovals);
    });
    row.append(goToApprovals);
    box.append(row);
    box.scrollTop = box.scrollHeight;
  }

  function chatHandleIncoming(msg) {
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "reply") {
      chatAppendMessage("assistant", msg.text || "");
    } else if (msg.type === "system") {
      chatAppendMessage("system", msg.text || "");
    } else if (msg.type === "tool_note") {
      chatAppendMessage("tool-note", msg.text || "");
    } else if (msg.type === "approval_card") {
      const approval = msg.approval || {};
      if (msg.auto_approved) {
        //: 已經被使用者預先寫好的自動核准規則（或 web 一步生效）核准並執行
        //: 過了——不是模型自己核准（聊天/agent 通道永遠沒有 approve/reject
        //: 工具）。已經不是 pending，沒有東西需要人再審一次，因此只留一行
        //: 狀態，不是完整卡片。
        chatAppendMessage("system", `已直接執行（任務 #${approval.id}）`);
      } else {
        chatAppendApprovalCard(approval);
      }
    }
  }

  function chatConnect() {
    if (!state.me) return;
    state.chatReconnectEnabled = true;
    if (state.chatReconnectTimer) {
      clearTimeout(state.chatReconnectTimer);
      state.chatReconnectTimer = null;
    }
    if (
      state.chatSocket
      && (state.chatSocket.readyState === WebSocket.CONNECTING || state.chatSocket.readyState === WebSocket.OPEN)
    ) {
      return;
    }
    chatSetStatus(false, "連線中…");
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}${CHAT_WEBSOCKET_PATH}`);
    const socketGeneration = state.generation;
    state.chatSocket = ws;
    state.chatSocketGeneration = socketGeneration;

    ws.addEventListener("open", () => {
      if (
        state.chatSocket !== ws
        || socketGeneration !== state.generation
        || !state.chatReconnectEnabled
      ) {
        ws.close();
        return;
      }
      state.chatReconnectDelay = 1000;
      if (state.legacyToken) {
        ws.send(JSON.stringify({ type: "auth", token: state.legacyToken }));
      }
      chatSetStatus(true, "已連線");
    });

    ws.addEventListener("message", (event) => {
      if (
        state.chatSocket !== ws
        || socketGeneration !== state.generation
        || !state.chatReconnectEnabled
      ) return;
      try {
        chatHandleIncoming(JSON.parse(event.data));
      } catch (_error) {
        // 非合法 JSON 的訊息直接忽略，不讓整條連線掛掉。
      }
    });

    ws.addEventListener("close", () => {
      if (state.chatSocket !== ws || socketGeneration !== state.generation) return;
      state.chatSocket = null;
      if (!state.chatReconnectEnabled) return;
      chatSetStatus(false, `連線中斷，${Math.round(state.chatReconnectDelay / 1000)} 秒後重試…`);
      state.chatReconnectTimer = setTimeout(chatConnect, state.chatReconnectDelay);
      state.chatReconnectDelay = Math.min(state.chatReconnectDelay * 2, 15000);
    });

    ws.addEventListener("error", () => {
      if (state.chatSocket === ws && socketGeneration === state.generation) ws.close();
    });
  }

  function chatStop(statusText = "已停用") {
    state.chatReconnectEnabled = false;
    if (state.chatReconnectTimer) {
      clearTimeout(state.chatReconnectTimer);
      state.chatReconnectTimer = null;
    }
    const socket = state.chatSocket;
    state.chatSocket = null;
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
      socket.close();
    }
    chatSetStatus(false, statusText);
  }

  function chatSend() {
    const input = element("assistant-input");
    const text = input.value.trim();
    if (!text) return;
    if (!state.chatSocket || state.chatSocket.readyState !== WebSocket.OPEN) {
      showAlert("助手連線尚未就緒，請稍候再試");
      return;
    }
    input.value = "";
    chatAppendMessage("user", text);
    state.chatSocket.send(JSON.stringify({ type: "chat", text }));
  }

  function renderDatasetState() {
    const target = element("dataset-state");
    target.replaceChildren();
    const datasetState = state.workspace.recent_dataset_assets;
    if (datasetState.state !== "available") {
      target.append(
        node("strong", "Dataset Assets 未啟用"),
        node("span", String(datasetState.reason || datasetState.state))
      );
      return;
    }
    if (!datasetState.items.length) {
      target.append(
        node("strong", "沒有可見的 Dataset Asset"),
        node("span", "Owned 與已核准 shared assets 都會依 Project scope 顯示。")
      );
      return;
    }
    for (const asset of datasetState.items) {
      const card = node("article", null, "item-card");
      const header = node("div", null, "item-card-header");
      header.append(
        node("h3", String(asset.name)),
        node("span", String(asset.access_mode || "owned"), "honesty-label")
      );
      card.append(
        header,
        node("p", String(asset.description || "（無描述）")),
        node(
          "span",
          `${asset.snapshot_count} snapshots · ${asset.active_alias_count} aliases · scope ${asset.scope_project_id}`,
          "item-meta"
        )
      );
      target.append(card);
    }
  }

  function datasetPublishEligibleProjects() {
    if (!state.workspace || !state.me || !state.me.actor) return [];
    const platformAdmin = Boolean(state.me.actor.platform_admin);
    return state.workspace.projects.filter((project) =>
      platformAdmin || (project.roles || []).includes("dataset_manager")
    );
  }

  function datasetPublishLocalPathEnabled() {
    const capability = state.workspace
      && state.workspace.capabilities
      && state.workspace.capabilities.dataset_publish;
    return Boolean(
      capability
      && capability.enabled
      && capability.local_path_enabled === true
    );
  }

  function renderDatasetPublishSourceFields() {
    const local = element("dataset-publish-local-fields");
    const run = element("dataset-publish-run-fields");
    const sourceSelect = element("dataset-publish-source-kind");
    const localOption = element("dataset-publish-source-local-path");
    const localPathEnabled = datasetPublishLocalPathEnabled();
    const localWasSelected = sourceSelect.value === "local_path";
    localOption.disabled = !localPathEnabled;
    element("dataset-publish-local-path-note").hidden = localPathEnabled;
    if (!localPathEnabled) {
      sourceSelect.value = "run_output";
      element("dataset-publish-local-path").value = "";
      if (localWasSelected) {
        state.datasetPublishPreview = null;
        state.datasetPublishRequestKey = null;
      }
    }
    const localSelected = localPathEnabled && sourceSelect.value === "local_path";
    local.hidden = !localSelected;
    run.hidden = localSelected;
    element("dataset-publish-local-path").disabled = !localSelected;
    element("dataset-publish-plan-id").disabled = localSelected;
    element("dataset-publish-output-name").disabled = localSelected;
  }

  function renderDatasetPublishWizard() {
    const panel = element("dataset-publish-panel");
    const capability = state.workspace
      && state.workspace.capabilities
      && state.workspace.capabilities.dataset_publish;
    const enabled = Boolean(capability && capability.enabled);
    panel.hidden = !enabled;
    if (!enabled) {
      state.datasetPublishPreview = null;
      state.datasetPublishRequestKey = null;
      element("dataset-publish-local-path").value = "";
      return;
    }

    const projects = datasetPublishEligibleProjects();
    const projectSelect = element("dataset-publish-project");
    const selectedProject = projectSelect.value;
    projectSelect.replaceChildren();
    for (const project of projects) {
      const option = node("option", project.name);
      option.value = project.id;
      projectSelect.append(option);
    }
    if (projects.some((project) => project.id === selectedProject)) {
      projectSelect.value = selectedProject;
    }
    projectSelect.disabled = projects.length === 0;
    renderDatasetPublishSourceFields();
    renderDatasetPublishPreview();
    if (!projects.length) {
      element("dataset-publish-status").textContent =
        "目前 caller 沒有 Dataset Manager role；伺服器不會接受 publish request。";
      element("dataset-publish-preview-btn").disabled = true;
    }
  }

  function datasetPublishOptionalText(id) {
    const value = element(id).value.trim();
    return value || null;
  }

  function datasetPublishCounts() {
    const raw = element("dataset-publish-counts").value.trim();
    if (!raw) return [];
    const items = raw.split(",").map((item) => item.trim());
    const counts = items.map((item) => {
      const match = /^([A-Za-z0-9][A-Za-z0-9._-]{0,63})=(0|[1-9][0-9]*)$/.exec(item);
      if (!match) {
        throw new Error("Counts 必須使用 canonical name=non-negative integer 格式。");
      }
      const value = Number(match[2]);
      if (!Number.isSafeInteger(value)) {
        throw new Error("Count 超出瀏覽器可精確表示的整數範圍。");
      }
      return { name: match[1], value };
    });
    counts.sort((left, right) => left.name.localeCompare(right.name));
    if (new Set(counts.map((item) => item.name)).size !== counts.length) {
      throw new Error("Data Card count name 不可重複。");
    }
    return counts;
  }

  function datasetPublishRequestBody() {
    const sourceKind = element("dataset-publish-source-kind").value;
    if (sourceKind === "local_path" && !datasetPublishLocalPathEnabled()) {
      throw new Error("Local path publish 未由管理員設定 allowlist；請使用 completed Run output。");
    }
    const source = sourceKind === "local_path"
      ? {
          kind: "local_path",
          path: element("dataset-publish-local-path").value.trim(),
        }
      : {
          kind: "run_output",
          plan_id: element("dataset-publish-plan-id").value.trim(),
          output_declaration_name: element("dataset-publish-output-name").value.trim(),
        };
    return {
      source,
      asset: {
        name: element("dataset-publish-asset-name").value.trim(),
        description: element("dataset-publish-description").value.trim(),
        data_card: {
          collection_method: element("dataset-publish-collection").value.trim(),
          processing_method: element("dataset-publish-processing").value.trim(),
          license: datasetPublishOptionalText("dataset-publish-license"),
          use_restrictions: datasetPublishOptionalText("dataset-publish-restrictions"),
          sensitive_data: datasetPublishOptionalText("dataset-publish-sensitive"),
          known_issues: datasetPublishOptionalText("dataset-publish-issues"),
          recommended_use: datasetPublishOptionalText("dataset-publish-recommended"),
          counts: datasetPublishCounts(),
        },
      },
      initial_alias: datasetPublishOptionalText("dataset-publish-alias"),
    };
  }

  function renderDatasetPublishPreview() {
    const preview = state.datasetPublishPreview;
    const status = element("dataset-publish-status");
    const findings = element("dataset-publish-findings");
    const contract = element("dataset-publish-contract");
    const requestButton = element("dataset-publish-request-btn");
    findings.replaceChildren();
    if (!preview) {
      status.textContent = "尚未建立 preview。";
      contract.hidden = true;
      element("dataset-publish-contract-json").textContent = "";
      requestButton.disabled = true;
      return;
    }
    status.textContent =
      "Preview ready；request 會重新掃描 source，digest 不一致時零持久化並回 409。";
    const assetName = element("dataset-publish-asset-name").value.trim();
    appendDetail(findings, "Asset 名稱", assetName || "-");
    appendDetail(findings, "來源類型", preview.source.kind);
    appendDetail(
      findings,
      "清單摘要碼",
      typeof preview.manifest_digest === "string" ? preview.manifest_digest.slice(0, 12) : "-"
    );
    appendDetail(findings, "檔案數／位元組", `${preview.file_count} / ${preview.total_bytes}`);
    appendDetail(
      findings,
      "分片策略",
      `${preview.shard_policy.max_shard_files} 個檔案 · ${preview.shard_policy.max_shard_bytes} 位元組`
    );
    appendDetail(findings, "預覽摘要碼", preview.preview_digest);
    element("dataset-publish-contract-json").textContent = JSON.stringify(preview, null, 2);
    contract.hidden = false;
    requestButton.disabled = false;
  }

  function invalidateDatasetPublishPreview() {
    if (!state.datasetPublishPreview) return;
    state.datasetPublishPreview = null;
    state.datasetPublishRequestKey = null;
    renderDatasetPublishPreview();
    element("dataset-publish-status").textContent = "欄位已變更；請重新建立唯讀 preview。";
  }

  async function previewDatasetPublish(event) {
    event.preventDefault();
    clearAlert();
    const button = element("dataset-publish-preview-btn");
    const projectId = element("dataset-publish-project").value;
    if (!projectId) return;
    button.disabled = true;
    element("dataset-publish-status").textContent = "正在安全掃描 source；尚未寫入任何資料…";
    try {
      const body = datasetPublishRequestBody();
      const preview = await productMutation(
        `/api/v2/projects/${projectId}/dataset-publish-previews`,
        body,
        { idempotency: false }
      );
      if (!preview || preview.project_id !== projectId || typeof preview.preview_digest !== "string") {
        throw new Error("Dataset publish preview response contract mismatch");
      }
      if (body.source.kind === "local_path" && JSON.stringify(preview).includes(body.source.path)) {
        throw new Error("Dataset publish preview disclosed the absolute local source path");
      }
      state.datasetPublishPreview = preview;
      state.datasetPublishRequestKey = randomUUID();
      renderDatasetPublishPreview();
    } catch (error) {
      state.datasetPublishPreview = null;
      state.datasetPublishRequestKey = null;
      renderDatasetPublishPreview();
      showAlert(error instanceof Error ? error.message : "無法建立 Dataset publish preview");
    } finally {
      button.disabled = datasetPublishEligibleProjects().length === 0;
    }
  }

  async function requestDatasetPublish() {
    const preview = state.datasetPublishPreview;
    if (!preview) return;
    const button = element("dataset-publish-request-btn");
    button.disabled = true;
    clearAlert();
    element("dataset-publish-status").textContent = "正在重掃 source 並建立 pending approval…";
    try {
      const result = await productMutation(
        `/api/v2/projects/${preview.project_id}/dataset-publish-requests`,
        Object.assign(datasetPublishRequestBody(), {
          expected_preview_digest: preview.preview_digest,
        }),
        { idempotencyKey: state.datasetPublishRequestKey }
      );
      state.datasetPublishPreview = null;
      state.datasetPublishRequestKey = null;
      renderDatasetPublishPreview();
      await initialize();
      activateSection("approvals");
      showAlert(`Dataset publish approval #${result.approval_id} 已建立；尚未發佈任何 asset。`);
    } catch (error) {
      button.disabled = false;
      element("dataset-publish-status").textContent =
        "Request 未完成；source drift 時請重新建立 preview，其餘錯誤可用相同 idempotency identity 安全重試。";
      showAlert(error instanceof Error ? error.message : "無法建立 Dataset publish approval");
    }
  }

  function renderSessions() {
    const container = element("session-list");
    container.replaceChildren();
    if (!state.sessions.length) {
      container.append(emptyState("沒有 server session", "目前 principal 可能使用 service 或 legacy credential。"));
    }
    for (const session of state.sessions) {
      const card = node("article", null, "item-card");
      const header = node("div", null, "item-card-header");
      header.append(node("h3", session.current ? "目前 session" : "瀏覽器 Session"));
      header.append(node("span", session.state, "honesty-label"));
      card.append(header);
      const meta = node("div", null, "item-meta");
      meta.append(
        node("span", `核發 · ${formatTimestamp(session.created_at)}`),
        node("span", `到期 · ${formatTimestamp(session.expires_at)}`),
        node("span", `來源 · ${session.authentication_source}`)
      );
      if (session.revoked_at) meta.append(node("span", `已撤銷 · ${formatTimestamp(session.revoked_at)}`));
      card.append(meta);
      container.append(card);
    }
    const more = element("more-sessions-btn");
    more.hidden = !state.nextSessionsCursor;
    more.disabled = false;
  }

  function commaSeparatedValues(id) {
    const values = element(id).value
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
    return Array.from(new Set(values)).sort();
  }

  function bootstrapRequestBody() {
    if (!state.me || !state.me.actor || !state.me.actor.platform_admin) {
      throw new Error("Project Wizard 只開放給 Platform Admin。");
    }
    const requesterId = state.me.actor.id;
    const reviewerId = element("bootstrap-reviewer-id").value.trim();
    if (reviewerId === requesterId) {
      throw new Error("Independent reviewer 必須是另一位 enabled human。");
    }
    const tags = commaSeparatedValues("bootstrap-tags");
    const nonSecretEnv = commaSeparatedValues("bootstrap-non-secret-env");
    const secretReferences = commaSeparatedValues("bootstrap-secret-references");
    const executable = element("bootstrap-executable").value.trim();
    const script = element("bootstrap-script").value.trim();
    const preflightChecks = [
      { kind: "executable_present", name: executable },
      { kind: "project_relative_path_present", path: script },
      ...tags.map((name) => ({ kind: "server_tag_present", name })),
      ...nonSecretEnv.map((name) => ({ kind: "non_secret_env_present", name })),
      ...secretReferences.map((name) => ({ kind: "secret_reference_present", name })),
    ];
    const outputName = element("bootstrap-output-name").value.trim();
    const outputPath = element("bootstrap-output-path").value.trim();
    if (Boolean(outputName) !== Boolean(outputPath)) {
      throw new Error("Output name 與 relative path 必須同時填寫或同時留空。");
    }
    const name = element("bootstrap-name").value.trim();
    return {
      project_id: element("bootstrap-project-id").value,
      name,
      slug: name,
      source: {
        kind: element("bootstrap-source-kind").value,
        reference: element("bootstrap-source-reference").value.trim(),
      },
      role_assignments: [
        {
          actor_id: requesterId,
          roles: ["owner", "operator", "dataset_manager"],
        },
        { actor_id: reviewerId, roles: ["reviewer"] },
      ],
      environment: {
        name: element("bootstrap-environment-name").value.trim(),
        setup_command: element("bootstrap-setup-command").value,
        required_server_tags: tags,
        working_directory_policy: "project_checkout",
        non_secret_env: nonSecretEnv,
        secret_references: secretReferences,
        preflight_checks: preflightChecks,
      },
      run_template: {
        name: element("bootstrap-template-name").value.trim(),
        argv_template: [
          { kind: "literal", value: executable },
          { kind: "literal", value: script },
        ],
        parameter_schema: [],
        resource_requirements: {
          required_tags: tags,
          min_gpu_count: Number(element("bootstrap-gpu-count").value),
          min_gpu_memory_mb: Number(element("bootstrap-gpu-memory").value),
          min_available_ram_mb: 0,
          min_available_disk_mb: 0,
          exclusive_worker: true,
        },
        output_declarations: outputName
          ? [{ name: outputName, kind: "file", path_pattern: outputPath, required: true }]
          : [],
      },
      defaults: { parameter_values: {} },
      dataset_grants: [],
      dataset_aliases: [],
    };
  }

  function renderBootstrapPreview() {
    const preview = state.bootstrapPreview;
    const status = element("bootstrap-status");
    const findings = element("bootstrap-findings");
    const contract = element("bootstrap-contract");
    const requestButton = element("bootstrap-request-btn");
    findings.replaceChildren();
    if (!preview) {
      status.textContent = "尚未建立 preview。";
      contract.hidden = true;
      element("bootstrap-payload-summary").replaceChildren();
      requestButton.disabled = true;
      return;
    }
    status.textContent = preview.blocking
      ? "Preview 有 blocking findings；不會建立 approval。"
      : "Preview ready；送出時會提交完全相同的 immutable payload。";
    appendDetail(findings, "內容摘要碼", preview.payload_digest);
    appendDetail(findings, "RBAC 就緒狀態", preview.readiness.ready ? "就緒" : "被擋住");
    appendDetail(
      findings,
      "檢查結果",
      preview.findings.length ? preview.findings.join(", ") : "無"
    );
    const payload = preview.payload || {};
    const runTemplate = payload.run_template || {};
    const argvPreview = Array.isArray(runTemplate.argv_template)
      ? runTemplate.argv_template.map((part) => part && part.value).filter(Boolean).join(" ")
      : "-";
    const bootstrapSummary = element("bootstrap-payload-summary");
    bootstrapSummary.replaceChildren();
    appendDetail(bootstrapSummary, "專案名稱", payload.name || "-");
    appendDetail(bootstrapSummary, "Environment 名稱", (payload.environment && payload.environment.name) || "-");
    appendDetail(bootstrapSummary, "Template 名稱", runTemplate.name || "-");
    appendDetail(bootstrapSummary, "指令預覽", argvPreview || "-");
    appendDetail(
      bootstrapSummary,
      "參數數量",
      String(Array.isArray(runTemplate.parameter_schema) ? runTemplate.parameter_schema.length : 0)
    );
    element("bootstrap-contract-json").textContent = JSON.stringify(preview.payload, null, 2);
    contract.hidden = false;
    requestButton.disabled = Boolean(preview.blocking);
  }

  function invalidateBootstrapPreview() {
    if (!state.bootstrapPreview) return;
    state.bootstrapPreview = null;
    state.bootstrapRequestKey = null;
    element("bootstrap-status").textContent = "欄位已變更；請重新建立 preview。";
    element("bootstrap-findings").replaceChildren();
    element("bootstrap-payload-summary").replaceChildren();
    element("bootstrap-contract").hidden = true;
    element("bootstrap-request-btn").disabled = true;
  }

  async function previewBootstrap(event) {
    event.preventDefault();
    clearAlert();
    const button = element("bootstrap-preview-btn");
    button.disabled = true;
    element("bootstrap-status").textContent = "正在建立唯讀 preview…";
    try {
      state.bootstrapPreview = await productMutation(
        "/api/v2/projects/bootstrap-previews",
        bootstrapRequestBody(),
        { idempotency: false }
      );
      state.bootstrapRequestKey = randomUUID();
      renderBootstrapPreview();
    } catch (error) {
      state.bootstrapPreview = null;
      state.bootstrapRequestKey = null;
      renderBootstrapPreview();
      showAlert(error instanceof Error ? error.message : "無法建立 Bootstrap preview");
    } finally {
      button.disabled = false;
    }
  }

  async function requestBootstrap() {
    if (!state.bootstrapPreview || state.bootstrapPreview.blocking) return;
    const button = element("bootstrap-request-btn");
    button.disabled = true;
    clearAlert();
    element("bootstrap-status").textContent = "正在建立 pending approval…";
    try {
      const result = await productMutation(
        "/api/v2/projects/bootstrap-requests",
        {
          payload: state.bootstrapPreview.payload,
          expected_payload_digest: state.bootstrapPreview.payload_digest,
        },
        { idempotencyKey: state.bootstrapRequestKey }
      );
      element("bootstrap-status").textContent = `Approval #${result.approval_id} · ${result.status}`;
      await initialize();
      activateSection("approvals");
      showAlert(`Bootstrap approval #${result.approval_id} 已建立，等待另一位管理員決定。`);
    } catch (error) {
      button.disabled = false;
      element("bootstrap-status").textContent = "Request 未完成；可使用相同頁面狀態安全重試。";
      showAlert(error instanceof Error ? error.message : "無法建立 Bootstrap approval");
    }
  }

  function openBootstrapWizard() {
    if (!element("bootstrap-project-id").value) {
      element("bootstrap-project-id").value = randomUUID();
    }
    activateSection("project-bootstrap");
    element("bootstrap-name").focus();
  }

  function renderWorkspace() {
    renderRoleBadges();
    configureRoleAwareNavigation();
    renderSummary();
    renderCapabilities();
    renderIdentityDetails();
    renderProjects();
    renderRuns();
    renderJobDispatchProjectOptions();
    renderApprovals();
    renderDatasetState();
    renderDatasetPublishWizard();
    renderRunCreateForm();
    if (!state.runCreateProjectId) {
      const projects = runCreateEligibleProjects();
      if (projects.length && runCreationCapabilityEnabled()) {
        loadRunCreateWorkspace(projects[0].id);
      }
    }
    renderSessions();
    renderBootstrapPreview();
  }

  function clearWorkspace() {
    //: DG-UI-UNIFICATION v1 U7: identical teardown point to legacy's
    //: `handleUnauthorizedResponse()`/logout calling `stopProtectedActivity()`
    //: -- an expired/cleared identity must not leave an actor-bound chat
    //: socket open.
    chatStop("尚未登入");
    state.me = null;
    state.workspace = null;
    state.sessions = [];
    state.nextSessionsCursor = null;
    state.projectWorkspace = null;
    state.runCreateProjectId = null;
    state.runCreateWorkspace = null;
    state.runCreateAsset = null;
    state.runCreatePreview = null;
    state.runCreateRequestKey = null;
    ++state.runCreatePreviewSerial;
    state.approvalDetail = null;
    state.approvalDetailReviewed = false;
    state.selectedRunId = null;
    state.runDetail = null;
    state.runArtifacts = null;
    state.runComparison = null;
    state.runStopRequestKey = null;
    state.datasetPublishPreview = null;
    state.datasetPublishRequestKey = null;
    state.jobs = [];
    state.jobsLoaded = false;
    state.openJobLogId = null;
    state.infraServerConfigs = [];
    state.infraServerConfigsLoaded = false;
    state.infraServers = {};
    state.infraIdleSummary = null;
    state.infraCandidates = [];
    state.infraCandidatesLoaded = false;
    state.infraImportCandidateId = null;
    state.infraCodexRunnerStatus = null;
    state.infraCodexRunnerConnectionFailed = false;
    element("role-badges").replaceChildren();
    element("summary-cards").replaceChildren();
    element("capability-list").replaceChildren();
    element("identity-details").replaceChildren();
    element("project-list").replaceChildren();
    element("run-list").replaceChildren();
    element("run-detail-panel").hidden = true;
    element("run-compare-left").value = "";
    element("run-compare-right").value = "";
    renderRunComparison();
    element("approval-list").replaceChildren();
    element("session-list").replaceChildren();
    element("more-sessions-btn").hidden = true;
    element("project-workspace-panel").hidden = true;
    element("run-create-panel").hidden = true;
    element("run-create-contract").hidden = true;
    element("run-create-findings").replaceChildren();
    element("run-create-parameter-summary").textContent = "";
    element("approval-review-panel").hidden = true;
    element("open-bootstrap-btn").hidden = true;
    element("dataset-publish-panel").reset();
    element("dataset-publish-panel").hidden = true;
    renderDatasetPublishSourceFields();
    document.querySelector('[data-role-navigation="bootstrap"]').hidden = true;
    element("jobs-tbody").replaceChildren();
    element("job-log-panel").hidden = true;
    renderJobDispatchProjectOptions();
    element("infra-workers-tbody").replaceChildren();
    element("infra-idle-tbody").replaceChildren();
    element("infra-candidate-list").replaceChildren();
    element("infra-codex-status").replaceChildren();
    element("infra-server-form").hidden = true;
    element("infra-manual-add-form").hidden = true;
    element("infra-import-form").hidden = true;
    element("assistant-messages").replaceChildren();
  }

  function assistantSectionIsActive() {
    const active = document.querySelector("#workspace-navigation button.active");
    return Boolean(active && active.getAttribute("data-section") === "assistant");
  }

  async function initialize() {
    //: DG-UI-UNIFICATION v1 U7: a full identity re-init (refresh button,
    //: legacy-token-btn toggle) must not leave a chat socket bound to the
    //: previous identity silently unauthenticated in the background --
    //: mirrors legacy `initializeBrowserAuthentication()` tearing down and
    //: reconnecting `chatSocket` on every call. `chatConnect()`/`chatStop()`
    //: themselves stay section-gated (see `activateSection()`); this only
    //: reconnects when 助手 already happens to be the visible section.
    const wasAssistantActive = assistantSectionIsActive();
    if (wasAssistantActive) chatStop("正在確認登入狀態…");
    const generation = ++state.generation;
    state.approvalDetail = null;
    state.approvalDetailReviewed = false;
    state.authenticationModeKnown = false;
    clearAlert();
    setAuthenticationControls();
    element("identity-status").textContent = "正在確認登入狀態…";
    try {
      const me = await productRead("/api/v2/me");
      if (generation !== state.generation) return;
      state.me = me;
      state.oidcEnabled = Boolean(me.authentication && me.authentication.oidc_enabled);
      state.authenticationModeKnown = true;
      const [workspace, sessions] = await Promise.all([
        productRead("/api/v2/workspace"),
        productRead("/api/v2/me/sessions?limit=25"),
      ]);
      if (generation !== state.generation) return;
      state.workspace = workspace;
      state.sessions = Array.isArray(sessions.items) ? sessions.items : [];
      state.nextSessionsCursor = sessions.next_cursor || null;
      setAuthenticationControls();
      renderWorkspace();
      if (wasAssistantActive) chatConnect();
    } catch (error) {
      if (generation !== state.generation) return;
      clearWorkspace();
      if (error instanceof RequestFailure) {
        state.oidcEnabled = error.oidcEnabled;
        state.authenticationModeKnown = error.authenticationModeKnown;
        if (error.status === 401) {
          state.legacyToken = "";
          setAuthenticationControls();
          showAlert(state.oidcEnabled
            ? "需要先使用 OIDC 登入。"
            : "需要先登入；只有已核准的 legacy-only rollback 才可暫時輸入 token。"
          );
          return;
        }
      }
      setAuthenticationControls();
      showAlert(error instanceof Error ? error.message : "無法載入 My Workspace");
    }
  }

  async function loadMoreSessions() {
    if (!state.nextSessionsCursor) return;
    const button = element("more-sessions-btn");
    button.disabled = true;
    try {
      const page = await productRead(
        `/api/v2/me/sessions?limit=25&cursor=${encodeURIComponent(state.nextSessionsCursor)}`
      );
      state.sessions.push(...(Array.isArray(page.items) ? page.items : []));
      state.nextSessionsCursor = page.next_cursor || null;
      renderSessions();
    } catch (error) {
      button.disabled = false;
      showAlert(error instanceof Error ? error.message : "無法載入更多 sessions");
    }
  }

  function activateSection(section) {
    //: DG-UI-UNIFICATION v1 U7: the 助手 WS connection is section-scoped --
    //: connect only while the section is visible, stop the moment the user
    //: navigates away (mirrors legacy's page-load-scoped socket, narrowed to
    //: this one section instead of the whole app).
    const previousActive = document.querySelector("#workspace-navigation button.active");
    const previousSection = previousActive && previousActive.getAttribute("data-section");
    if (previousSection === "assistant" && section !== "assistant") chatStop("尚未連線");
    for (const candidate of document.querySelectorAll("[data-workspace-section]")) {
      candidate.hidden = candidate.getAttribute("data-workspace-section") !== section;
    }
    for (const button of document.querySelectorAll("#workspace-navigation button")) {
      const active = button.getAttribute("data-section") === section;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    }
    window.location.hash = section === "overview" ? "" : `section/${section}`;
    element("workspace-main").focus({ preventScroll: true });
    //: DG-UI-UNIFICATION v1 U3: jobs are not part of the `/api/v2/workspace`
    //: projection `initialize()` already loads, so this section polls its
    //: own list -- once on activation, again only via the section's own
    //: refresh button (no global 5s timer).
    if (section === "jobs" && state.me) loadJobs();
    //: DG-UI-UNIFICATION v1 U4: same reasoning -- infrastructure is not part
    //: of the `/api/v2/workspace` projection, so each sub-panel loads once
    //: on activation and otherwise only via its own refresh button.
    if (section === "infrastructure" && state.me) {
      loadInfraServers();
      loadInfraIdleSummary();
      loadInfraCandidates();
      loadInfraCodexRunnerStatus();
    }
    //: DG-UI-UNIFICATION v1 U5: legacy-projects summary/matrix and legacy
    //: datasets are not part of the `/api/v2/workspace` projection either --
    //: same once-on-activation reasoning as jobs/infrastructure above.
    if (section === "projects" && state.me && !state.legacyProjectsSummaryLoaded) {
      loadLegacyProjectsSummary();
    }
    if (section === "legacy-datasets" && state.me) loadLegacyDatasets();
    //: DG-UI-UNIFICATION v1 U6a: AI 工程 is not part of the
    //: `/api/v2/workspace` projection either -- same once-on-activation
    //: reasoning as jobs/infrastructure/projects above.
    if (section === "engineering" && state.me) loadEngineeringTasks();
    //: DG-UI-UNIFICATION v1 U7: same once-on-activation reasoning as
    //: jobs/infrastructure/projects/engineering above -- chat is not part of
    //: the `/api/v2/workspace` projection and connects fresh per activation.
    if (section === "assistant" && state.me) chatConnect();
  }

  function sectionFromHash() {
    const match = /^#section\/(overview|projects|project-bootstrap|runs|jobs|engineering|infrastructure|legacy-datasets|approvals|datasets|sessions|assistant)$/.exec(window.location.hash);
    return match ? match[1] : "overview";
  }

  function installEventHandlers() {
    element("workspace-navigation").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-section]");
      if (button && !button.hidden) activateSection(button.getAttribute("data-section"));
    });
    element("refresh-btn").addEventListener("click", initialize);
    element("jobs-refresh-btn").addEventListener("click", loadJobs);
    element("job-dispatch-form").addEventListener("submit", submitJobDispatch);
    for (const radio of document.querySelectorAll('input[name="job-dispatch-server-mode"]')) {
      radio.addEventListener("change", renderJobDispatchServerFieldState);
    }
    element("engineering-refresh-btn").addEventListener("click", loadEngineeringTasks);
    element("engineering-create-btn").addEventListener("click", openEngineeringWizard);
    element("engineering-wizard-close-btn").addEventListener("click", closeEngineeringWizard);
    element("engineering-wizard-project").addEventListener("change", (event) => {
      loadEngineeringWizardVersions(event.target.value);
    });
    element("engineering-wizard-panel").addEventListener("input", updateEngineeringWizardPreview);
    element("engineering-wizard-panel").addEventListener("change", updateEngineeringWizardPreview);
    element("engineering-wizard-coverage-btn").addEventListener("click", checkEngineeringWizardPathCoverage);
    element("engineering-wizard-submit-btn").addEventListener("click", submitEngineeringWizard);
    element("engineering-task-detail-close-btn").addEventListener("click", closeEngineeringTaskDetail);
    element("engineering-task-detail-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-task-tab]");
      if (button) activateEngineeringTaskTab(button.getAttribute("data-task-tab"));
    });
    element("job-log-close-btn").addEventListener("click", closeJobLogPanel);
    element("job-log-panel").addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeJobLogPanel();
      }
    });
    element("infra-workers-refresh-btn").addEventListener("click", loadInfraServers);
    element("infra-server-form-toggle-btn").addEventListener("click", openServerFormForAdd);
    element("infra-server-form-cancel-btn").addEventListener("click", closeServerForm);
    element("infra-server-test-ssh-btn").addEventListener("click", testServerFormSsh);
    element("infra-server-submit-btn").addEventListener("click", submitServerForm);
    element("infra-idle-refresh-btn").addEventListener("click", loadInfraIdleSummary);
    element("infra-idle-hours").addEventListener("change", loadInfraIdleSummary);
    element("infra-scan-btn").addEventListener("click", scanInventoryAction);
    element("infra-candidate-refresh-btn").addEventListener("click", loadInfraCandidates);
    element("infra-manual-add-toggle-btn").addEventListener("click", openManualAddForm);
    element("infra-manual-add-cancel-btn").addEventListener("click", closeManualAddForm);
    element("infra-manual-add-submit-btn").addEventListener("click", submitManualAddAction);
    element("infra-ignore-nested-btn").addEventListener("click", ignoreNestedCandidatesAction);
    element("infra-import-cancel-btn").addEventListener("click", closeImportForm);
    element("infra-import-submit-btn").addEventListener("click", submitImportAction);
    element("infra-codex-refresh-btn").addEventListener("click", loadInfraCodexRunnerStatus);
    //: DG-UI-UNIFICATION v1 U5: 新增專案／矩陣／專案詳情面板／時間軸紀錄
    //: 表單／資料集 section 事件綁定。
    element("legacy-project-create-toggle-btn").addEventListener("click", () => {
      renderLegacyProjectCreateForm(element("legacy-project-create-form").hidden);
    });
    element("legacy-project-create-cancel-btn").addEventListener("click", () => {
      renderLegacyProjectCreateForm(false);
    });
    element("legacy-project-create-submit-btn").addEventListener("click", submitLegacyProjectCreate);
    element("legacy-matrix-refresh-btn").addEventListener("click", loadLegacyProjectsSummary);
    element("legacy-project-detail-close-btn").addEventListener("click", closeLegacyProjectDetail);
    element("legacy-project-detail-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-legacy-detail-tab]");
      if (button) activateLegacyProjectDetailTab(button.getAttribute("data-legacy-detail-tab"));
    });
    element("legacy-project-detail-docs-submit-btn").addEventListener("click", submitLegacyProjectDocs);
    element("legacy-project-delete-btn").addEventListener("click", () => {
      if (state.legacyProjectDetailName) deleteLegacyProjectAction(state.legacyProjectDetailName);
    });
    element("legacy-project-deploy-submit-btn").addEventListener("click", submitLegacyProjectDeploy);
    element("legacy-project-timeline-q").addEventListener("change", () => loadLegacyProjectTimeline(true));
    element("legacy-project-timeline-more-btn").addEventListener("click", () => loadLegacyProjectTimeline(false));
    element("legacy-project-record-submit-btn").addEventListener("click", submitLegacyProjectRecord);
    element("legacy-project-activity-probe-btn").addEventListener("click", probeLegacyProjectActivity);
    //: DG-UI-UNIFICATION v1 U6b: AI Engineer tab -- Development Session
    //: workbench + per-project AI conversation event bindings.
    element("legacy-agent-session-refresh-btn").addEventListener("click", () => {
      if (state.agentSessionProjectName) loadAgentSessionPanel(state.agentSessionProjectName);
    });
    element("legacy-agent-session-open-form").addEventListener("submit", submitAgentSessionOpen);
    element("legacy-agent-session-close-btn").addEventListener("click", agentSessionCloseCurrent);
    element("legacy-agent-session-form").addEventListener("submit", submitAgentSessionMessage);
    element("legacy-agent-session-diff-btn").addEventListener("click", loadAgentSessionDiff);
    element("legacy-agent-session-checkpoint-btn").addEventListener("click", submitAgentSessionCheckpoint);
    element("legacy-agent-session-promote-btn").addEventListener("click", submitAgentSessionPromote);
    element("legacy-ai-conversation-refresh-btn").addEventListener("click", () => {
      if (state.aiConversationProjectName) loadAIConversationPanel(state.aiConversationProjectName);
    });
    element("legacy-ai-conversation-form").addEventListener("submit", submitAIConversationMessage);
    element("legacy-dataset-refresh-btn").addEventListener("click", loadLegacyDatasets);
    element("legacy-dataset-create-submit-btn").addEventListener("click", submitLegacyDatasetCreate);
    element("legacy-dataset-card-close-btn").addEventListener("click", closeLegacyDatasetCard);
    element("legacy-dataset-card-update-submit-btn").addEventListener("click", submitLegacyDatasetCardUpdate);
    element("open-bootstrap-btn").addEventListener("click", openBootstrapWizard);
    element("bootstrap-form").addEventListener("submit", previewBootstrap);
    element("bootstrap-form").addEventListener("input", invalidateBootstrapPreview);
    element("bootstrap-request-btn").addEventListener("click", requestBootstrap);
    element("dataset-publish-panel").addEventListener("submit", previewDatasetPublish);
    element("dataset-publish-panel").addEventListener("input", invalidateDatasetPublishPreview);
    element("dataset-publish-source-kind").addEventListener("change", () => {
      renderDatasetPublishSourceFields();
      invalidateDatasetPublishPreview();
    });
    element("dataset-publish-request-btn").addEventListener("click", requestDatasetPublish);
    element("run-create-panel").addEventListener("submit", previewRunCreate);
    element("run-create-panel").addEventListener("input", (event) => {
      if (event.target === element("run-create-project")) return;
      invalidateRunCreatePreview();
      renderRunCreateForm();
    });
    element("run-create-project").addEventListener("change", (event) => {
      loadRunCreateWorkspace(event.target.value);
    });
    element("run-create-project-version").addEventListener("change", () => {
      invalidateRunCreatePreview();
      renderRunCreateTargetOptions();
      renderRunCreateForm();
    });
    element("run-create-template").addEventListener("change", () => {
      invalidateRunCreatePreview();
      renderRunCreateForm();
    });
    element("run-create-target").addEventListener("change", () => {
      invalidateRunCreatePreview();
      renderRunCreateForm();
    });
    element("run-create-dataset-mode").addEventListener("change", () => {
      invalidateRunCreatePreview();
      renderRunCreateForm();
    });
    element("run-create-asset").addEventListener("change", () => {
      invalidateRunCreatePreview();
      loadRunCreateAsset();
    });
    element("run-create-dataset-selection-kind").addEventListener("change", () => {
      invalidateRunCreatePreview();
      renderRunCreateForm();
    });
    element("run-create-dataset-selection").addEventListener("change", () => {
      invalidateRunCreatePreview();
      renderRunCreateForm();
    });
    element("run-create-request-btn").addEventListener("click", requestRunCreate);
    element("run-instance-update-btn").addEventListener("click", requestSelectedInstanceUpdate);
    element("run-clone-btn").addEventListener("click", previewRunClone);
    element("run-stop-btn").addEventListener("click", requestRunStop);
    element("run-artifacts-btn").addEventListener("click", () => loadRunArtifacts(state.selectedRunId));
    element("run-compare-btn").addEventListener("click", compareRuns);
    element("approval-review-confirm").addEventListener("change", (event) => {
      state.approvalDetailReviewed = Boolean(event.target.checked);
      element("approval-review-approve").disabled = (
        !state.approvalDetailReviewed || state.approvalDetailOneTimeSecret
      );
    });
    element("approval-review-approve").addEventListener("click", (event) => {
      decideReviewedApproval("approve", event.currentTarget);
    });
    element("approval-review-reject").addEventListener("click", (event) => {
      decideReviewedApproval("reject", event.currentTarget);
    });
    element("more-sessions-btn").addEventListener("click", loadMoreSessions);
    element("assistant-form").addEventListener("submit", (event) => {
      event.preventDefault();
      chatSend();
    });
    element("assistant-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        chatSend();
      }
    });
    element("sign-in-btn").addEventListener("click", () => {
      const parameters = new URLSearchParams({ return_to: "/" });
      window.location.assign(`/auth/login?${parameters.toString()}`);
    });
    element("legacy-token-btn").addEventListener("click", () => {
      if (state.legacyToken) {
        state.legacyToken = "";
      } else {
        const supplied = window.prompt("輸入暫時 X-Auth-Token；重新整理後會消失：", "");
        if (supplied === null) return;
        state.legacyToken = supplied.trim();
      }
      initialize();
    });
    element("logout-btn").addEventListener("click", async () => {
      state.generation += 1;
      state.legacyToken = "";
      clearWorkspace();
      setAuthenticationControls();
      try {
        await authenticationRequest("/auth/logout", { method: "POST" });
      } catch (_error) {
        // Local state is already cleared; the reload asks the server again.
      } finally {
        window.location.replace("/");
      }
    });
    window.addEventListener("hashchange", () => activateSection(sectionFromHash()));
  }

  installEventHandlers();
  activateSection(sectionFromHash());
  initialize();
})();
