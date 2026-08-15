(function () {
  "use strict";

  const PRODUCT_READ_PATHS = new Set([
    "/api/v2/me",
    "/api/v2/me/sessions",
    "/api/v2/workspace",
  ]);
  const PRODUCT_MUTATION_PATHS = new Set([
    "/api/v2/projects/bootstrap-previews",
    "/api/v2/projects/bootstrap-requests",
  ]);
  const PROJECT_WORKSPACE_PATH = /^\/api\/v2\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/workspace$/;
  const DATASET_PUBLISH_MUTATION_PATH = /^\/api\/v2\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/dataset-publish-(previews|requests)$/;
  const APPROVAL_DETAIL_PATH = /^\/api\/v2\/approvals\/[1-9][0-9]*$/;
  const APPROVAL_DECISION_PATH = /^\/api\/v2\/approvals\/[1-9][0-9]*\/decisions$/;
  const PRODUCT_RUN_DETAIL_PATH = /^\/api\/v2\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const PRODUCT_RUN_ARTIFACT_PATH = /^\/api\/v2\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/artifacts$/;
  const PRODUCT_RUN_MUTATION_PATH = /^\/api\/v2\/runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/(clone-previews|stop-requests)$/;
  const PRODUCT_RUN_COMPARE_PATH = "/api/v2/runs/compare";
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
    "dataset_publish_v2",
    ...DATASET_SHARING_APPROVAL_KINDS,
    "execution_plan_v2",
    "stop",
  ]);
  const INSPECTABLE_APPROVAL_KINDS = new Set([
    ...REVIEWED_APPROVAL_KINDS,
    "project_role_change",
  ]);
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
    approvalDetail: null,
    approvalDetailReviewed: false,
    selectedRunId: null,
    runDetail: null,
    runArtifacts: null,
    runComparison: null,
    runStopRequestKey: null,
    generation: 0,
  };

  class RequestFailure extends Error {
    constructor(response, body) {
      const apiMessage = body && body.error && body.error.message;
      const legacyMessage = body && body.detail;
      super(apiMessage || legacyMessage || `Request failed (${response.status})`);
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
      || APPROVAL_DETAIL_PATH.test(parsed.pathname)
      || PRODUCT_RUN_DETAIL_PATH.test(parsed.pathname)
      || PRODUCT_RUN_ARTIFACT_PATH.test(parsed.pathname)
      || parsed.pathname === PRODUCT_RUN_COMPARE_PATH;
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
      || APPROVAL_DECISION_PATH.test(parsed.pathname)
      || PRODUCT_RUN_MUTATION_PATH.test(parsed.pathname);
    if (parsed.origin !== window.location.origin || parsed.search || !reviewedPath) {
      throw new Error("Unreviewed Product mutation path");
    }
    const headers = Object.assign(requestHeaders(), { "Content-Type": "application/json" });
    if (!options || options.idempotency !== false) {
      headers["Idempotency-Key"] = options && options.idempotencyKey
        ? options.idempotencyKey
        : randomUUID();
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
      container.append(node("span", "Platform Admin", "role-badge"));
    }
    const roles = new Set();
    for (const project of state.me.project_roles || []) {
      for (const role of project.roles || []) roles.add(String(role));
    }
    for (const role of Array.from(roles).sort()) {
      container.append(node("span", role.replaceAll("_", " "), "role-badge"));
    }
    if (!container.childElementCount) {
      container.append(node("span", "No project role", "role-badge"));
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
      summaryCard("Scoped Projects", workspace.projects.length, "Server-authorized"),
      summaryCard(
        "Recent Runs",
        workspace.recent_runs.length,
        workspace.capabilities.run_experience_v2.enabled
          ? "ExecutionPlan projection"
          : "Legacy Job adapter"
      ),
      summaryCard("Pending Approvals", workspace.pending_approvals.length, "Viewable by caller"),
      summaryCard("Dataset Assets", workspace.recent_dataset_assets.items.length, "Scoped owned/shared")
    );
  }

  function renderCapabilities() {
    const container = element("capability-list");
    container.replaceChildren();
    for (const [key, capability] of Object.entries(state.workspace.capabilities || {})) {
      const row = node("div", null, "capability-row");
      const description = node("div");
      description.append(node("strong", key.replaceAll("_", " ")));
      description.append(node("small", capability.reason || "runtime configuration"));
      const pill = node("span", String(capability.state), "state-pill");
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

  function renderIdentityDetails() {
    const details = element("identity-details");
    details.replaceChildren();
    const me = state.me;
    appendDetail(details, "Actor type", me.actor.type);
    appendDetail(details, "Authentication", me.authentication.method);
    appendDetail(details, "Authorization", me.authorization.mode);
    appendDetail(details, "Role model", me.authorization.role_model);
    appendDetail(details, "OIDC", me.authentication.oidc_enabled ? "enabled" : "disabled");
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
    appendDetail(details, "Project UUID", workspace.project.id);
    appendDetail(details, "RBAC readiness", workspace.rbac.state);
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
    appendDetail(details, "Dataset grants", workspace.dataset_grants.state);
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
      header.append(node("h3", project.name), node("span", (project.roles || []).join(" · "), "honesty-label"));
      card.append(header, node("p", project.summary || "尚未提供摘要。"));
      const meta = node("div", null, "item-meta");
      meta.append(node("span", `Project ID · ${project.id}`), node("span", formatTimestamp(project.created_at)));
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
        node("span", `${artifact.kind} · ${artifact.size_bytes} bytes`),
        node("small", `SHA-256 ${artifact.sha256} · ${formatTimestamp(artifact.reported_at)}`)
      );
      target.append(card);
    }
    const honesty = [
      `availability ${page.availability}`,
      "metadata only",
      page.complete ? "complete" : "collection completeness unknown",
      page.truncated ? "truncated" : null,
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
      ? "Verified ExecutionPlan v2 projection；canonical Job/attempt evidence remains authoritative."
      : "Limited legacy projection；unknown dimensions and partial lineage are preserved.";
    const meta = element("run-detail-meta");
    appendDetail(meta, "Project", `${detail.project_name} · ${detail.project_id}`);
    appendDetail(meta, "Contract", `${detail.contract.kind} · ${detail.contract.version || "unknown"}`);
    appendDetail(meta, "Canonical Job", detail.canonical_job_status || "not materialized");
    appendDetail(
      meta,
      "Current attempt",
      detail.current_attempt
        ? `${detail.current_attempt.state} · liveness ${detail.current_attempt.liveness}`
        : "none"
    );
    appendDetail(
      meta,
      "Attention",
      detail.attention_reasons.length ? detail.attention_reasons.join(", ") : "none"
    );
    appendDetail(meta, "Created", formatTimestamp(detail.created_at));
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
        emptyState("無法載入 artifact metadata", error instanceof Error ? error.message : "Unknown error")
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
      const equal = dimension.equal === null || dimension.equal === undefined
        ? "unknown"
        : dimension.equal ? "equal" : "different";
      card.append(
        node("strong", name.replaceAll("_", " ")),
        node("span", `${dimension.availability} · ${equal}`)
      );
      if (dimension.availability === "known") {
        const summary = node("pre");
        summary.textContent = JSON.stringify({ left: dimension.left, right: dimension.right }, null, 2);
        card.append(summary);
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
        emptyState("無法比較 Product Runs", error instanceof Error ? error.message : "Unknown error")
      );
    } finally {
      button.disabled = false;
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
      header.append(node("h3", `#${approval.id} · ${approval.kind}`));
      const disposition = approval.can_decide ? "可決定" : "僅可查看";
      header.append(node("span", disposition, "honesty-label"));
      card.append(header);
      const meta = node("div", null, "item-meta");
      meta.append(
        node("span", approval.project_id ? `Project · ${approval.project_id}` : "Platform scope"),
        node("span", formatTimestamp(approval.created_at)),
        node("span", approval.requester_is_self ? "Caller requested" : "Another requester")
      );
      card.append(meta);
      if (INSPECTABLE_APPROVAL_KINDS.has(approval.kind)) {
        const actions = node("div", null, "button-row");
        const review = node("button", "檢視完整 immutable contract", "button button-quiet");
        review.type = "button";
        review.addEventListener("click", () => loadApprovalDetail(approval.id, review));
        actions.append(review);
        card.append(actions);
      }
      container.append(card);
    }
    renderApprovalDetail();
  }

  function renderApprovalDetail() {
    const detail = state.approvalDetail;
    const panel = element("approval-review-panel");
    const meta = element("approval-review-meta");
    const payload = element("approval-review-payload");
    const confirmRow = element("approval-review-confirm-row");
    const confirm = element("approval-review-confirm");
    const actions = element("approval-review-actions");
    const approve = element("approval-review-approve");
    const reject = element("approval-review-reject");
    meta.replaceChildren();
    payload.textContent = "";
    confirm.checked = false;
    state.approvalDetailReviewed = false;
    approve.disabled = true;
    approve.dataset.idempotencyKey = "";
    reject.dataset.idempotencyKey = "";
    if (!detail) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    element("approval-review-title").textContent = `#${detail.id} · ${detail.kind}`;
    element("approval-review-verification").textContent = detail.payload_verified
      ? "Digest verified"
      : "Unverified";
    appendDetail(meta, "Contract version", detail.payload_contract_version);
    appendDetail(meta, "Payload digest", detail.payload_digest);
    appendDetail(meta, "Requester", detail.requester_actor_id);
    appendDetail(meta, "Status", detail.status);
    payload.textContent = JSON.stringify(detail.payload, null, 2);
    const canDecideReviewed = REVIEWED_APPROVAL_KINDS.has(detail.kind) && detail.status === "pending" && detail.can_decide && detail.payload_verified;
    const approveLabels = {
      project_bootstrap_v2: "核准 Project Bootstrap",
      environment_change_v2: "核准 Environment revision",
      run_template_change_v2: "核准 Run Template revision",
      project_defaults_change_v2: "核准 Project Defaults revision",
      dataset_publish_v2: "核准 Dataset Publish",
      dataset_share_offer_v2: "核准 Dataset Share Offer",
      dataset_share_accept_v2: "核准 Dataset Share Accept",
      dataset_grant_revoke_v2: "核准 Dataset Grant 變更",
      execution_plan_v2: "核准 ExecutionPlan v2",
      stop: "核准 Stop request",
    };
    approve.textContent = approveLabels[detail.kind] || "核准 immutable contract";
    confirmRow.hidden = !canDecideReviewed;
    actions.hidden = !canDecideReviewed;
  }

  async function loadApprovalDetail(approvalId, button) {
    button.disabled = true;
    state.approvalDetail = null;
    renderApprovalDetail();
    clearAlert();
    try {
      const detail = await productRead(`/api/v2/approvals/${approvalId}`);
      if (!detail || detail.id !== approvalId || detail.payload_verified !== true) {
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
    if (!detail || !REVIEWED_APPROVAL_KINDS.has(detail.kind) || detail.payload_verified !== true) return;
    if (decision === "approve" && !state.approvalDetailReviewed) return;
    button.disabled = true;
    clearAlert();
    try {
      const idempotencyKey = button.dataset.idempotencyKey || randomUUID();
      button.dataset.idempotencyKey = idempotencyKey;
      const result = await productMutation(
        `/api/v2/approvals/${detail.id}/decisions`,
        { decision, note: "Reviewed in Product v2 approval detail" },
        { idempotencyKey }
      );
      await initialize();
      const destination = detail.kind === "dataset_publish_v2" || DATASET_SHARING_APPROVAL_KINDS.has(detail.kind)
        ? "datasets"
        : ["execution_plan_v2", "stop"].includes(detail.kind) ? "runs" : "projects";
      activateSection(destination);
      if (result.project_id && detail.kind !== "dataset_publish_v2" && !DATASET_SHARING_APPROVAL_KINDS.has(detail.kind) && !["execution_plan_v2", "stop"].includes(detail.kind)) {
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
      } else if (decision === "approve" && detail.kind === "dataset_share_offer_v2") {
        showAlert(`Dataset Share Offer ${result.offer_id} 已建立。`);
      } else if (decision === "approve" && detail.kind === "dataset_share_accept_v2") {
        showAlert(`Dataset Share Offer ${result.offer_id} 已接受；已建立 ${(result.grant_ids || []).length} 個 grant。`);
      } else if (decision === "approve" && detail.kind === "dataset_grant_revoke_v2") {
        showAlert(`Dataset Grant ${result.grant_id} 已完成 ${result.operation || "變更"}。`);
      } else if (decision === "approve" && detail.kind === "execution_plan_v2") {
        showAlert(`ExecutionPlan ${result.execution_plan_id} 已核准；Job ${result.job_id} 仍由 canonical scheduler 管理。`);
      } else if (decision === "approve" && detail.kind === "stop") {
        showAlert(`Stop approval #${detail.id} 已核准；Run 顯示 stopping，尚未宣稱 terminal。`);
        if (result.plan_id) await loadRunDetail(result.plan_id);
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
        node("p", String(asset.description || "No description")),
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
    appendDetail(findings, "Preview digest", preview.preview_digest);
    appendDetail(findings, "Source kind", preview.source.kind);
    appendDetail(findings, "Manifest digest", preview.manifest_digest);
    appendDetail(findings, "Files / bytes", `${preview.file_count} / ${preview.total_bytes}`);
    appendDetail(
      findings,
      "Shard policy",
      `${preview.shard_policy.max_shard_files} files · ${preview.shard_policy.max_shard_bytes} bytes`
    );
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
      header.append(node("h3", session.current ? "目前 session" : "Browser session"));
      header.append(node("span", session.state, "honesty-label"));
      card.append(header);
      const meta = node("div", null, "item-meta");
      meta.append(
        node("span", `Issued · ${formatTimestamp(session.created_at)}`),
        node("span", `Expires · ${formatTimestamp(session.expires_at)}`),
        node("span", `Source · ${session.authentication_source}`)
      );
      if (session.revoked_at) meta.append(node("span", `Revoked · ${formatTimestamp(session.revoked_at)}`));
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
      requestButton.disabled = true;
      return;
    }
    status.textContent = preview.blocking
      ? "Preview 有 blocking findings；不會建立 approval。"
      : "Preview ready；送出時會提交完全相同的 immutable payload。";
    appendDetail(findings, "Payload digest", preview.payload_digest);
    appendDetail(findings, "RBAC readiness", preview.readiness.ready ? "ready" : "blocked");
    appendDetail(
      findings,
      "Findings",
      preview.findings.length ? preview.findings.join(", ") : "none"
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
    renderApprovals();
    renderDatasetState();
    renderDatasetPublishWizard();
    renderSessions();
    renderBootstrapPreview();
  }

  function clearWorkspace() {
    state.me = null;
    state.workspace = null;
    state.sessions = [];
    state.nextSessionsCursor = null;
    state.projectWorkspace = null;
    state.approvalDetail = null;
    state.approvalDetailReviewed = false;
    state.selectedRunId = null;
    state.runDetail = null;
    state.runArtifacts = null;
    state.runComparison = null;
    state.runStopRequestKey = null;
    state.datasetPublishPreview = null;
    state.datasetPublishRequestKey = null;
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
    element("approval-review-panel").hidden = true;
    element("open-bootstrap-btn").hidden = true;
    element("dataset-publish-panel").reset();
    element("dataset-publish-panel").hidden = true;
    renderDatasetPublishSourceFields();
    document.querySelector('[data-role-navigation="bootstrap"]').hidden = true;
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
  }

  function sectionFromHash() {
    const match = /^#section\/(overview|projects|project-bootstrap|runs|approvals|datasets|sessions)$/.exec(window.location.hash);
    return match ? match[1] : "overview";
  }

  function installEventHandlers() {
    element("workspace-navigation").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-section]");
      if (button && !button.hidden) activateSection(button.getAttribute("data-section"));
    });
    element("refresh-btn").addEventListener("click", initialize);
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
    element("run-clone-btn").addEventListener("click", previewRunClone);
    element("run-stop-btn").addEventListener("click", requestRunStop);
    element("run-artifacts-btn").addEventListener("click", () => loadRunArtifacts(state.selectedRunId));
    element("run-compare-btn").addEventListener("click", compareRuns);
    element("approval-review-confirm").addEventListener("change", (event) => {
      state.approvalDetailReviewed = Boolean(event.target.checked);
      element("approval-review-approve").disabled = !state.approvalDetailReviewed;
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
