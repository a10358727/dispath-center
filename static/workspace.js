(function () {
  "use strict";

  const PRODUCT_READ_PATHS = new Set([
    "/api/v2/me",
    "/api/v2/me/sessions",
    "/api/v2/workspace",
    //: DG-UI-UNIFICATION v1 U3: the job-collection surface has no id
    //: segment, so it is a literal like the three above rather than a regex.
    "/api/v2/jobs",
  ]);
  const PRODUCT_MUTATION_PATHS = new Set([
    "/api/v2/projects/bootstrap-previews",
    "/api/v2/projects/bootstrap-requests",
    //: DG-UI-UNIFICATION v1 U3: builds a `kind=enqueue` Approval on the
    //: legacy Job/Approval model (see `dispatch_center/api/routers/jobs_v2.py`).
    "/api/v2/dispatch-requests",
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
      || JOBS_READ_PATH.test(parsed.pathname);
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
      || JOBS_MUTATION_PATH.test(parsed.pathname);
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
    const response = await fetch(parsed.pathname, {
      method: "POST",
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
      const actions = node("div", null, "button-row");
      const open = node("button", "查看 Workspace", "button button-quiet");
      open.type = "button";
      open.addEventListener("click", () => loadProjectWorkspace(project.id));
      actions.append(open);
      card.append(actions);
      container.append(card);
    }
    renderProjectWorkspace();
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
      actionsCell.append(node("span", "查看 AI 工程任務（尚未實作連結）", "honesty-label"));
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
  }

  async function initialize() {
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
  }

  function sectionFromHash() {
    const match = /^#section\/(overview|projects|project-bootstrap|runs|jobs|approvals|datasets|sessions)$/.exec(window.location.hash);
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
    element("job-log-close-btn").addEventListener("click", closeJobLogPanel);
    element("job-log-panel").addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeJobLogPanel();
      }
    });
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
