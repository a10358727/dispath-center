(function () {
  "use strict";

  const flags = window.DISPATCH_UI_FLAGS || {};
  const INSTRUCTION_LIMIT = 4000;
  const TASK_EVENT_PAGE_SIZE = 100;
  const TAB_LABELS = Object.freeze({
    overview: "總覽",
    projects: "專案",
    work: "工作",
    approvals: "核准",
    datasets: "資料與結果",
    jobs: "Runtime Jobs",
    validation: "Validation",
    servers: "基礎設施",
    activity: "活動與稽核",
    administration: "管理",
    "coding-runs": "AI 工程任務",
    chat: "助手",
  });
  const ROLE_LABELS = Object.freeze({
    admin: "Project Admin",
    operator: "Operator",
    viewer: "Viewer",
  });
  const ROLE_PRIORITY = Object.freeze({ viewer: 1, operator: 2, admin: 3 });
  const PROJECT_WORKSPACE_SECTIONS = Object.freeze([
    "overview",
    "code-version",
    "ai-engineering",
    "runs-validation",
    "data-artifacts",
    "settings",
    "deployment",
  ]);
  const PROJECT_WORKSPACE_SECTION_LABELS = Object.freeze({
    overview: "概覽",
    "code-version": "程式碼與版本",
    "ai-engineering": "AI 工程",
    "runs-validation": "執行與驗證",
    "data-artifacts": "資料與產物",
    settings: "設定",
    deployment: "部署",
  });
  const PROJECT_WORKSPACE_ROLE_COPY = Object.freeze({
    viewer: {
      label: "Viewer",
      guidance: "優先檢視狀態、日誌、Diff 與結果。角色提示不會隱藏任何既有操作。",
    },
    operator: {
      label: "Operator",
      guidance: "優先處理 AI 工程任務、Runtime Jobs 與驗證。所有 mutation 仍由伺服器核准。",
    },
    admin: {
      label: "Project Admin",
      guidance: "優先檢視專案設定、memberships 與 promotion；目前 presentation 不形成授權閘門。",
    },
    platform_admin: {
      label: "Platform Admin",
      guidance: "可同時關注 Runner、workers 與平台設定；操作是否合法仍由後端決定。",
    },
    service: {
      label: "Service actor",
      guidance: "這是 service actor 與 scope 的展示，不使用『以人身分核准』的文案。",
    },
    none: {
      label: "No project role",
      guidance: "目前未回傳此專案角色；authorization 仍為 off/shadow，不以瀏覽器隱藏操作。",
    },
  });
  const ENGINEERING_TASK_STATUS = Object.freeze({
    pending_approval: { label: "等待核准", icon: "⌛" },
    queued: { label: "排隊中", icon: "…" },
    staging: { label: "正在準備基準", icon: "◌" },
    running: { label: "執行中", icon: "●" },
    finalizing: { label: "整理結果中", icon: "◌" },
    done: { label: "完成", icon: "✓" },
    no_changes: { label: "完成，無變更", icon: "=" },
    failed: { label: "失敗", icon: "×" },
    staging_failed: { label: "基準準備失敗", icon: "×" },
    secret_violation: { label: "安全政策拒絕", icon: "⛨" },
    path_policy_violation: { label: "路徑政策拒絕", icon: "⊘" },
    blocked: { label: "被擋住", icon: "!" },
    cancelled: { label: "已取消", icon: "—" },
    rejected: { label: "未核准", icon: "⊘" },
    interrupted: { label: "已中斷", icon: "◇" },
    disconnected: { label: "連線中斷", icon: "↯" },
    discarded: { label: "已作廢", icon: "⊘" },
    unknown: { label: "狀態未知", icon: "?" },
    passed: { label: "通過", icon: "✓" },
    not_run: { label: "未執行", icon: "—" },
  });

  const state = {
    initialized: false,
    currentAuthInfo: null,
    legacyOpenCodingTaskModal: null,
    targetProject: null,
    step: 1,
    opener: null,
    openSerial: 0,
    runnerStatus: null,
    runnerConnected: false,
    versions: [],
    backendCapabilities: null,
    backendCapabilitiesLoaded: false,
    codingAgentProviders: [],
    submitting: false,
    engineeringTasks: [],
    detailTaskId: null,
    detailTask: null,
    detailOpener: null,
    detailOpenSerial: 0,
    detailDiffLoaded: false,
    detailDiffLoading: false,
    detailEventCursor: null,
    detailEventsCanLoadMore: false,
    detailEventsLoading: false,
    detailEventsNotice: null,
    detailBackgroundSnapshot: null,
    listLoadSerial: 0,
    currentProject: null,
    currentProjectSection: "overview",
  };

  function element(id) {
    return document.getElementById(id);
  }

  function parseProjectWorkspaceRoute(hash) {
    const value = String(hash || "");
    if (!value.startsWith("#project/")) return null;
    const tail = value.slice("#project/".length);
    const separator = tail.indexOf("/");
    const encodedName = separator === -1 ? tail : tail.slice(0, separator);
    const requestedSection = separator === -1 ? "" : tail.slice(separator + 1);
    let name;
    try {
      name = decodeURIComponent(encodedName);
    } catch (_error) {
      return null;
    }
    if (!name) return null;
    if (!requestedSection) return { name, section: "overview" };
    const section = PROJECT_WORKSPACE_SECTIONS.includes(requestedSection)
      ? requestedSection
      : "overview";
    return { name, section };
  }

  function projectWorkspaceHash(projectName, section = "overview") {
    const base = `#project/${encodeURIComponent(String(projectName || ""))}`;
    return section && section !== "overview" && PROJECT_WORKSPACE_SECTIONS.includes(section)
      ? `${base}/${section}`
      : base;
  }

  function activateProjectWorkspaceSection(section, { focus = false } = {}) {
    const selected = PROJECT_WORKSPACE_SECTIONS.includes(section)
      ? section
      : "overview";
    const subnavigation = element("project-subnavigation");
    if (!subnavigation) return selected;
    subnavigation.querySelectorAll("[data-project-section]").forEach((control) => {
      const active = control.dataset.projectSection === selected;
      if (active) {
        control.setAttribute("aria-current", "page");
        control.scrollIntoView({ block: "nearest", inline: "nearest" });
      } else {
        control.removeAttribute("aria-current");
      }
    });
    document.querySelectorAll("[data-project-workspace-pane]").forEach((pane) => {
      pane.hidden = pane.dataset.projectWorkspacePane !== selected;
    });
    state.currentProjectSection = selected;
    if (focus) {
      const pane = element(`project-workspace-${selected}`);
      const heading = pane && pane.querySelector("h3");
      if (heading) {
        heading.tabIndex = -1;
        heading.focus({ preventScroll: true });
      }
    }
    return selected;
  }

  function resolveProjectWorkspaceRole(authInfo, project) {
    const actor = authInfo && authInfo.actor;
    if (!actor || !project || !project.id) return PROJECT_WORKSPACE_ROLE_COPY.none;
    if (actor.type === "service") return PROJECT_WORKSPACE_ROLE_COPY.service;
    if (actor.platform_admin) return PROJECT_WORKSPACE_ROLE_COPY.platform_admin;
    const memberships = Array.isArray(authInfo.project_memberships)
      ? authInfo.project_memberships
      : [];
    const membership = memberships.find(
      (candidate) => candidate && candidate.project_id === project.id
    );
    if (!membership) return PROJECT_WORKSPACE_ROLE_COPY.none;
    return PROJECT_WORKSPACE_ROLE_COPY[membership.role] || PROJECT_WORKSPACE_ROLE_COPY.none;
  }

  function renderProjectWorkspaceRole(project) {
    state.currentProject = project || null;
    const badge = element("pd-role-badge");
    const guidance = element("pd-role-guidance");
    if (!badge || !guidance || !project) return;
    const presentation = resolveProjectWorkspaceRole(state.currentAuthInfo, project)
      || PROJECT_WORKSPACE_ROLE_COPY.none;
    badge.textContent = presentation.label;
    badge.hidden = false;
    guidance.textContent = presentation.guidance;
  }

  function clearProjectWorkspaceRole() {
    state.currentProject = null;
    state.currentProjectSection = "overview";
    const badge = element("pd-role-badge");
    const guidance = element("pd-role-guidance");
    if (badge) {
      badge.textContent = "";
      badge.hidden = true;
    }
    if (guidance) {
      guidance.textContent = "角色只影響資訊提示，不是瀏覽器端授權控制。";
    }
  }

  function codePointLength(value) {
    return Array.from(String(value || "")).length;
  }

  function bulletItems(rawValue) {
    const trimmed = String(rawValue || "").trim();
    if (!trimmed) return [];
    return trimmed
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function compareUtf8(left, right) {
    const encoder = new TextEncoder();
    const a = encoder.encode(left);
    const b = encoder.encode(right);
    const length = Math.min(a.length, b.length);
    for (let index = 0; index < length; index += 1) {
      if (a[index] !== b[index]) return a[index] - b[index];
    }
    return a.length - b.length;
  }

  function pathScopeMatches(scope, path) {
    if (scope === ".") return true;
    if (scope.endsWith("/")) {
      return path === scope.slice(0, -1) || path.startsWith(scope);
    }
    return path === scope;
  }

  function canonicalPathItems(rawValue) {
    const unique = Array.from(new Set(bulletItems(rawValue))).sort(compareUtf8);
    if (unique.includes(".")) return ["."];
    const subtrees = [];
    unique.filter((item) => item.endsWith("/")).forEach((scope) => {
      const root = scope.slice(0, -1);
      if (!subtrees.some((parent) => pathScopeMatches(parent, root))) {
        subtrees.push(scope);
      }
    });
    const exact = unique.filter(
      (scope) => !scope.endsWith("/") &&
        !subtrees.some((parent) => pathScopeMatches(parent, scope))
    );
    return [...subtrees, ...exact].sort(compareUtf8);
  }

  function pathScopeError(scope) {
    const encoder = new TextEncoder();
    if (
      scope.normalize("NFC") !== scope ||
      /[\u0000-\u001f\u007f]/.test(scope) ||
      scope.startsWith("/") ||
      scope.startsWith("~") ||
      /^[A-Za-z]:/.test(scope) ||
      scope.includes("\\") ||
      encoder.encode(scope).length > 1024
    ) {
      return true;
    }
    if (scope === ".") return false;
    const path = scope.endsWith("/") ? scope.slice(0, -1) : scope;
    if (!path) return true;
    const components = path.split("/");
    return components.some(
      (component) => !component || component === "." || component === ".." ||
        component.toLocaleLowerCase("en-US") === ".git"
    );
  }

  function validatePathPolicyInputs(values) {
    const allowed = bulletItems(values.allowedPaths);
    const prohibited = bulletItems(values.prohibitedPaths);
    const all = [...allowed, ...prohibited];
    const encoder = new TextEncoder();
    if (all.length > 256 ||
        all.reduce((total, scope) => total + encoder.encode(scope).length, 0) > 24 * 1024) {
      return { field: "allowed", message: "Path policy 最多 256 項，總 UTF-8 大小不得超過 24 KiB。" };
    }
    const invalidAllowed = allowed.some(pathScopeError);
    if (invalidAllowed) {
      return {
        field: "allowed",
        message: "Allowed path 必須是 canonical relative POSIX path；目錄以 / 結尾，整個 repository 使用 .。",
      };
    }
    const invalidProhibited = prohibited.some(pathScopeError);
    if (invalidProhibited) {
      return {
        field: "prohibited",
        message: "Prohibited path 必須是 canonical relative POSIX path；不可使用 absolute、..、backslash、control 或 .git。",
      };
    }
    return null;
  }

  function addSection(parts, heading, items) {
    if (!items.length) return;
    parts.push(`${heading}:\n${items.map((item) => `- ${item}`).join("\n")}`);
  }

  /**
   * Deterministically render the only free-text value accepted by the current
   * backend. No value is interpreted as a command or inserted into a shell.
   */
  function renderStructuredInstruction(
    values,
    { enforceFinalGitPaths = false } = {}
  ) {
    const parts = ["AI Engineering Task"];
    addSection(parts, "Task objective", bulletItems(values.objective));
    addSection(parts, "Background and relevant context", bulletItems(values.background));
    addSection(parts, "Expected changes", bulletItems(values.expectedChanges));
    addSection(parts, "Non-goals", bulletItems(values.nonGoals));
    addSection(
      parts,
      "Allowed modification scope",
      enforceFinalGitPaths
        ? canonicalPathItems(values.allowedPaths)
        : bulletItems(values.allowedPaths)
    );
    addSection(
      parts,
      "Prohibited paths",
      enforceFinalGitPaths
        ? canonicalPathItems(values.prohibitedPaths)
        : bulletItems(values.prohibitedPaths)
    );
    addSection(parts, "Prohibited changes", bulletItems(values.prohibitedChanges));
    addSection(parts, "Acceptance criteria", bulletItems(values.acceptanceCriteria));

    const validation = [];
    if (values.runTests) {
      validation.push(
        "Run the repository's relevant tests and lint checks only inside this current sandboxed agent turn; the outer Runner will not execute repository code after the turn."
      );
    }
    if (values.runBuild) {
      validation.push(
        "Run relevant build and smoke checks only inside this current sandboxed agent turn."
      );
    }
    if (values.autoFix) {
      validation.push("Where feasible in the current agent turn, analyze and fix validation failures.");
    }
    if (values.workerValidation && values.validationTarget) {
      validation.push(
        `Record ${values.validationTarget} as a worker validation preference; do not assume a worker Job exists.`
      );
    }
    addSection(parts, "Validation strategy", validation);

    const execution = [
      "Modify project files only inside the existing isolated Git worktree.",
      "Dependency installation is not authorized by this form; do not install dependencies.",
      "External network access is not authorized by this form; do not access external networks.",
    ];
    execution.push(
      enforceFinalGitPaths
        ? "The final Git diff is technically checked against the approved path policy on the Runner before bundling and independently on Server A before acceptance."
        : "Allowed/prohibited path entries are advisory approval requirements in this compatibility mode; platform path enforcement is not available."
    );
    if (enforceFinalGitPaths) {
      execution.push(
        "This final-result policy does not claim turn-time filesystem confinement; temporary writes during the agent turn remain outside this guarantee."
      );
    }
    addSection(parts, "Requested execution behavior", execution);
    return parts.join("\n\n");
  }

  function readFormValues() {
    const workerValidation = element("engineering-worker-validation").checked;
    return {
      objective: element("engineering-objective").value.trim(),
      background: element("engineering-background").value.trim(),
      expectedChanges: element("engineering-expected-changes").value.trim(),
      nonGoals: element("engineering-non-goals").value.trim(),
      allowedPaths: element("engineering-allowed-scope").value.trim(),
      prohibitedPaths: element("engineering-prohibited-paths").value.trim(),
      prohibitedChanges: element("engineering-prohibited").value.trim(),
      acceptanceCriteria: element("engineering-acceptance").value.trim(),
      runTests: element("engineering-tests").checked,
      runBuild: element("engineering-build").checked,
      autoFix: element("engineering-auto-fix").checked,
      workerValidation,
      validationTarget: workerValidation
        ? element("engineering-validation-target").value
        : "",
      baseBranch: element("engineering-base-branch").value.trim(),
    };
  }

  function selectedVersion() {
    const selectedId = element("engineering-version-reference").value;
    return state.versions.find((version) => version.id === selectedId) || null;
  }

  function immutableBackendEnabled() {
    return Boolean(
      state.backendCapabilitiesLoaded &&
      state.backendCapabilities &&
      state.backendCapabilities.enabled
    );
  }

  function engineeringTaskContractVersion() {
    if (!immutableBackendEnabled()) return null;
    return String(state.backendCapabilities.contract_version || "");
  }

  function finalGitPathPolicyEnabled() {
    return engineeringTaskContractVersion() === "engineering-task-v2";
  }

  function applyBackendMode() {
    const enabled = immutableBackendEnabled();
    const pathPolicyEnabled = finalGitPathPolicyEnabled();
    const baseBranch = element("engineering-base-branch");
    baseBranch.disabled = enabled;
    if (enabled) baseBranch.value = "";
    element("engineering-base-branch-optional").textContent = enabled ? "不使用" : "選填";
    element("engineering-base-branch-help").textContent = enabled
      ? "Immutable workflow 只使用所選 ProjectVersion 的 exact commit，不在執行時解析 branch。"
      : "相容模式會在執行時解析這個 branch；留空則使用當時的 HEAD。";
    element("engineering-version-label").textContent = enabled
      ? "ProjectVersion（必填）"
      : "ProjectVersion 參考";
    element("engineering-version-help").textContent = enabled
      ? "後端會驗證此版本屬於目前專案，並把 exact commit 固定在 approval payload。"
      : "只供核准者參考，不會寫入 request，也不會固定本次執行 commit。";
    const alert = element("engineering-version-mode-alert");
    alert.className = `ui-alert ${enabled ? "ui-alert-success" : "ui-alert-warning"}`;
    element("engineering-version-mode-title").textContent = enabled
      ? (pathPolicyEnabled
          ? "Immutable Engineering Task v2 已啟用"
          : "Immutable ProjectVersion contract 已啟用")
      : "尚未綁定 immutable revision";
    element("engineering-version-mode-copy").textContent = enabled
      ? (pathPolicyEnabled
          ? "核准 payload 會固定 ProjectVersion ID、exact commit 與 final Git result path policy；Hub staging 在人工核准後才執行。"
          : "核准 payload 會固定 ProjectVersion ID 與 exact commit；此 contract 未宣告 v2 path policy。")
      : "相容模式仍使用 base_branch 執行時解析；不可把上方版本視為執行保證。";
    element("engineering-approval-mode-copy").textContent = enabled
      ? (pathPolicyEnabled
          ? "送出後建立 kind=coding_task pending approval；ProjectVersion 與 v2 path policy 已固定，但不會直接執行或自動建立 worker validation Job。"
          : "送出後建立 kind=coding_task pending approval；ProjectVersion 已固定，但 path entries 仍是 advisory。")
      : "送出後只建立 kind=coding_task pending approval；不會直接執行、建立 worker Job 或固定 ProjectVersion。";

    const allowedPaths = element("engineering-allowed-scope");
    allowedPaths.required = pathPolicyEnabled;
    allowedPaths.setAttribute("aria-required", pathPolicyEnabled ? "true" : "false");
    const allowedRequirement = element("engineering-allowed-scope-requirement");
    allowedRequirement.className = pathPolicyEnabled ? "required" : "optional";
    allowedRequirement.textContent = pathPolicyEnabled ? "必填" : "選填";
    element("engineering-path-policy-heading-copy").textContent = pathPolicyEnabled
      ? "Immutable v2 將下列 machine-readable path policy 固定在核准 payload，並檢查 final Git result。"
      : (enabled
          ? "此 immutable contract 未宣告 v2 path policy；路徑只作為代理與核准者的 advisory 要求。"
          : "Legacy Coding Task 只把路徑寫入代理需求，不會技術強制。");

    const policyControl = element("engineering-path-policy-permission");
    policyControl.checked = pathPolicyEnabled;
    const policyBadge = element("engineering-path-policy-permission-badge");
    policyBadge.className = `enforcement-badge ${pathPolicyEnabled ? "enforced" : "advisory"}`;
    policyBadge.textContent = pathPolicyEnabled ? "技術強制" : "代理要求";
    element("engineering-path-policy-permission-copy").textContent = pathPolicyEnabled
      ? "只對 final Git diff 技術強制：Runner pre-bundle 與 Server A pre-accept 都會檢查；不提供 turn-time filesystem confinement。"
      : (enabled
          ? "此 immutable contract 未宣告 v2；allowed/prohibited paths 只作為 advisory 要求。"
          : "Legacy Coding Task 只把 allowed/prohibited paths 當成 advisory 代理要求。");
    const summaryBadge = element("engineering-path-policy-summary-badge");
    summaryBadge.className = `enforcement-badge ${pathPolicyEnabled ? "enforced" : "advisory"}`;
    summaryBadge.textContent = pathPolicyEnabled ? "技術強制" : "代理要求";
    element("engineering-path-policy-summary-copy").textContent = pathPolicyEnabled
      ? "v2 對 final Git diff 執行 Runner pre-bundle 與 Server A pre-accept 雙重檢查；不是回合期間的 filesystem confinement。"
      : (enabled
          ? "此 immutable contract 未宣告 v2 path policy；路徑不會由平台技術強制。"
          : "Legacy allowed/prohibited paths 不會由平台技術強制。");
    element("engineering-path-coverage-btn").disabled = !pathPolicyEnabled;
    if (!pathPolicyEnabled) clearPathPolicyCoverageResult();
    updatePreview();
  }

  function clearPathPolicyCoverageResult() {
    const container = element("engineering-path-coverage-result");
    if (container) container.replaceChildren();
  }

  function renderPathPolicyCoverageRules(heading, rules) {
    const section = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = heading;
    section.append(title);
    if (!rules.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "（未設定）";
      section.append(empty);
      return section;
    }
    const list = document.createElement("ul");
    rules.forEach((rule) => {
      const item = document.createElement("li");
      const scope = document.createElement("code");
      scope.textContent = rule.scope;
      const hits = document.createElement("span");
      hits.textContent = ` — 命中 ${rule.file_hits} 個檔案`;
      item.append(scope, hits);
      if (rule.matches_existing_directory) {
        const warning = document.createElement("span");
        warning.className = "field-error";
        warning.textContent = `；exact 規則命中既有目錄名，是否想寫成「${rule.scope}/」？`;
        item.append(warning);
      }
      if (rule.secret_protected) {
        const warning = document.createElement("span");
        warning.className = "field-error";
        warning.textContent = "；此規則命中受保護的 secret 檔名樣式，final 結果一定會被拒絕。";
        item.append(warning);
      }
      list.append(item);
    });
    section.append(list);
    return section;
  }

  async function checkPathPolicyCoverage() {
    const container = element("engineering-path-coverage-result");
    const button = element("engineering-path-coverage-btn");
    if (!container || !button) return;
    if (!finalGitPathPolicyEnabled()) {
      container.textContent = "此 contract 未啟用 v2 path policy，無法預檢。";
      return;
    }
    const version = selectedVersion();
    if (!version) {
      container.textContent = "請先選擇 ProjectVersion 再檢查涵蓋範圍。";
      return;
    }
    const values = readFormValues();
    const validationError = validatePathPolicyInputs(values);
    if (validationError) {
      container.textContent = "Allowed／prohibited paths 格式無效，請先修正再檢查涵蓋範圍。";
      return;
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    container.textContent = "正在對照 pinned 版本的實際檔案…";
    try {
      const body = {
        project_version_id: version.id,
        allowed_paths: canonicalPathItems(values.allowedPaths),
        prohibited_paths: canonicalPathItems(values.prohibitedPaths),
      };
      const coverage = await api(
        `/projects/${encodeURIComponent(state.targetProject)}/engineering-tasks/path-policy-coverage`,
        { method: "POST", body: JSON.stringify(body) }
      );
      const summary = document.createElement("div");
      const meta = document.createElement("p");
      meta.className = "muted";
      meta.textContent = coverage.truncated
        ? `已掃描前 ${coverage.tree_file_count} 個檔案（此版本檔案數超過預檢上限，計數可能為下界）。`
        : `已對照 ${coverage.tree_file_count} 個檔案。`;
      summary.append(meta);
      summary.append(renderPathPolicyCoverageRules("Allowed paths", coverage.allowed || []));
      summary.append(renderPathPolicyCoverageRules("Prohibited paths", coverage.prohibited || []));
      container.replaceChildren(summary);
    } catch (error) {
      container.textContent = `涵蓋範圍檢查失敗：${String(error.message || error)}`;
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  function runnerAvailability() {
    if (!state.runnerConnected) {
      return {
        ready: false,
        kind: "disconnected",
        title: "Runner 狀態 disconnected",
        message: "無法取得 Coding Runner 狀態；連線恢復並重新檢查前不能送出。",
      };
    }
    const status = state.runnerStatus || {};
    const isPlatformAdmin = Boolean(
      state.currentAuthInfo &&
      state.currentAuthInfo.actor &&
      state.currentAuthInfo.actor.type !== "service" &&
      state.currentAuthInfo.actor.platform_admin
    );
    if (!status.configured) {
      return {
        ready: false,
        kind: "error",
        title: "Coding Runner 尚未設定",
        message: isPlatformAdmin
          ? "請依 README §13 設定 CODEX_RUNNER_SERVER，再重新檢查。"
          : "請聯絡 Platform Admin 完成 Coding Runner 設定。",
      };
    }
    if (!status.online) {
      return {
        ready: false,
        kind: "disconnected",
        title: `${status.server || "Coding Runner"} 離線`,
        message: "Runner offline 不代表 Codex 未安裝；請先恢復機器連線。",
      };
    }
    if (status.probe_status === "probe_failed") {
      return {
        ready: false,
        kind: "disconnected",
        title: "Runner 能力探測失敗",
        message: "Runner 機器已在線，但平台無法完成 Codex 能力探測；請恢復連線後重試。",
      };
    }
    if (!status.codex_installed) {
      return {
        ready: false,
        kind: "error",
        title: "Runner 未安裝 Codex",
        message: isPlatformAdmin
          ? "Runner 已在線；請依 README §13 安裝 Codex CLI。"
          : "Runner 已在線，但未偵測到 Codex；請聯絡 Platform Admin。",
      };
    }
    if (!status.authenticated) {
      return {
        ready: false,
        kind: "error",
        title: "Codex 尚未登入",
        message: isPlatformAdmin
          ? "請在 Coding Runner 完成 Codex login，再重新檢查。"
          : "請聯絡 Platform Admin 完成 Runner 的 Codex login。",
      };
    }
    if (status.busy) {
      return {
        ready: true,
        kind: "warning",
        title: `${status.server} 忙碌，但仍可送出`,
        message: `目前正在執行 coding job #${status.running_job_id == null ? "-" : status.running_job_id}；核准後可能排隊。`,
      };
    }
    return {
      ready: true,
      kind: "success",
      title: `${status.server} 可用`,
      message: `${status.codex_version || "Codex 版本未知"} · ${status.auth_mode || "登入模式未知"} · 空閒`,
    };
  }

  function renderRunnerState({ loading = false } = {}) {
    const container = element("engineering-runner-state");
    if (!container) return;

    const availability = loading
      ? {
          ready: false,
          kind: "loading",
          title: "正在確認 Coding Runner",
          message: "取得線上狀態、Codex 安裝與登入資訊。",
        }
      : runnerAvailability();

    container.className = `component-state state-${availability.kind}`;
    const icon = document.createElement("div");
    icon.className = "component-state-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = {
      loading: "◌",
      success: "✓",
      warning: "!",
      error: "×",
      disconnected: "↯",
    }[availability.kind] || "?";

    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = availability.title;
    const message = document.createElement("p");
    message.textContent = availability.message;
    copy.append(title, message);

    const retry = document.createElement("button");
    retry.id = "engineering-runner-retry-btn";
    retry.type = "button";
    retry.textContent = loading ? "檢查中…" : "重新檢查";
    retry.disabled = loading;
    retry.addEventListener("click", () => refreshRunnerStatus(state.openSerial));
    container.replaceChildren(icon, copy, retry);
    updatePreview();
  }

  function setGlobalError(message) {
    const error = element("engineering-task-global-error");
    error.textContent = message || "";
    error.hidden = !message;
  }

  function clearFieldErrors() {
    const objective = element("engineering-objective");
    const objectiveError = element("engineering-objective-error");
    objective.removeAttribute("aria-invalid");
    objectiveError.hidden = true;
    const allowedPaths = element("engineering-allowed-scope");
    const allowedPathsError = element("engineering-allowed-scope-error");
    allowedPaths.removeAttribute("aria-invalid");
    allowedPathsError.hidden = true;
    const prohibitedPaths = element("engineering-prohibited-paths");
    const prohibitedPathsError = element("engineering-prohibited-paths-error");
    prohibitedPaths.removeAttribute("aria-invalid");
    prohibitedPathsError.hidden = true;
    const validationTarget = element("engineering-validation-target");
    const validationError = element("engineering-validation-error");
    validationTarget.removeAttribute("aria-invalid");
    validationError.hidden = true;
    setGlobalError("");
  }

  function validateStep(step, { focus = true } = {}) {
    clearFieldErrors();
    if (step === 2 && !element("engineering-objective").value.trim()) {
      element("engineering-objective").setAttribute("aria-invalid", "true");
      element("engineering-objective-error").hidden = false;
      if (focus) element("engineering-objective").focus();
      return false;
    }
    if (
      step === 3 &&
      finalGitPathPolicyEnabled() &&
      !element("engineering-allowed-scope").value.trim()
    ) {
      element("engineering-allowed-scope").setAttribute("aria-invalid", "true");
      element("engineering-allowed-scope-error").textContent =
        "Immutable v2 任務必須至少指定一個 allowed path；若允許整個 repository，請明確填入 .。";
      element("engineering-allowed-scope-error").hidden = false;
      if (focus) element("engineering-allowed-scope").focus();
      return false;
    }
    if (step === 3 && finalGitPathPolicyEnabled()) {
      const issue = validatePathPolicyInputs(readFormValues());
      if (issue) {
        const input = issue.field === "prohibited"
          ? element("engineering-prohibited-paths")
          : element("engineering-allowed-scope");
        const error = issue.field === "prohibited"
          ? element("engineering-prohibited-paths-error")
          : element("engineering-allowed-scope-error");
        input.setAttribute("aria-invalid", "true");
        error.textContent = issue.message;
        error.hidden = false;
        if (focus) input.focus();
        return false;
      }
    }
    if (
      step === 4 &&
      element("engineering-worker-validation").checked &&
      !element("engineering-validation-target").value
    ) {
      element("engineering-validation-target").setAttribute("aria-invalid", "true");
      element("engineering-validation-error").hidden = false;
      if (focus) element("engineering-validation-target").focus();
      return false;
    }
    return true;
  }

  function validateAll({ focus = true } = {}) {
    if (!validateStep(2, { focus })) {
      showStep(2, { focusHeading: false });
      if (focus) element("engineering-objective").focus();
      return false;
    }
    if (!validateStep(3, { focus })) {
      showStep(3, { focusHeading: false });
      if (focus) element("engineering-allowed-scope").focus();
      return false;
    }
    if (!validateStep(4, { focus })) {
      showStep(4, { focusHeading: false });
      if (focus) element("engineering-validation-target").focus();
      return false;
    }
    const instruction = renderStructuredInstruction(readFormValues(), {
      enforceFinalGitPaths: finalGitPathPolicyEnabled(),
    });
    if (codePointLength(instruction) > INSTRUCTION_LIMIT) {
      showStep(5, { focusHeading: false });
      setGlobalError(`Generated instruction 超過 ${INSTRUCTION_LIMIT} 字元，請縮短需求內容。`);
      if (focus) element("engineering-task-global-error").focus();
      return false;
    }
    if (immutableBackendEnabled() && !selectedVersion()) {
      showStep(1, { focusHeading: false });
      setGlobalError("Immutable workflow 需要選擇一筆 ProjectVersion。請先同步 Hub 或選擇版本。");
      if (focus) element("engineering-version-reference").focus();
      return false;
    }
    const availability = runnerAvailability();
    if (!availability.ready) {
      showStep(1, { focusHeading: false });
      setGlobalError(`${availability.title}：${availability.message}`);
      if (focus) element("engineering-task-global-error").focus();
      return false;
    }
    return true;
  }

  function updatePreview() {
    const dialog = element("engineering-task-dialog");
    if (!dialog || dialog.hidden) return;
    const values = readFormValues();
    const instruction = renderStructuredInstruction(values, {
      enforceFinalGitPaths: finalGitPathPolicyEnabled(),
    });
    const count = codePointLength(instruction);
    const countElement = element("engineering-character-count");
    element("engineering-instruction-preview").textContent = instruction;
    countElement.textContent = `${count} / ${INSTRUCTION_LIMIT} 字元`;
    countElement.classList.toggle("is-over-limit", count > INSTRUCTION_LIMIT);

    element("engineering-preview-project").textContent = state.targetProject || "-";
    const availability = runnerAvailability();
    element("engineering-preview-runner").textContent = state.runnerConnected
      ? (state.runnerStatus && state.runnerStatus.server) || availability.title
      : "disconnected";
    element("engineering-preview-branch").textContent = immutableBackendEnabled()
      ? "不使用；由 exact commit 固定"
      : values.baseBranch || "執行時 HEAD（未固定）";
    const version = selectedVersion();
    element("engineering-preview-version").textContent = version
      ? `${version.git_commit || "unknown"}${immutableBackendEnabled() ? "（exact commit 已固定）" : "（只供參考，未綁定）"}`
      : "尚未固定 immutable revision";
    element("engineering-preview-path-policy").textContent = finalGitPathPolicyEnabled()
      ? "v2 final Git diff：Runner pre-bundle + Server A pre-accept"
      : (immutableBackendEnabled()
          ? "Immutable compatibility contract：advisory，未技術強制"
          : "Legacy advisory；未技術強制");
    element("engineering-preview-validation").textContent = values.validationTarget
      ? `${values.validationTarget}（偏好，不自動建 Job）`
      : "無";

    const canSubmit = Boolean(
      !state.submitting &&
      availability.ready &&
      values.objective &&
      (!finalGitPathPolicyEnabled() || Boolean(values.allowedPaths)) &&
      (!finalGitPathPolicyEnabled() || !validatePathPolicyInputs(values)) &&
      count <= INSTRUCTION_LIMIT &&
      state.backendCapabilitiesLoaded &&
      (!immutableBackendEnabled() || Boolean(version)) &&
      (!values.workerValidation || values.validationTarget)
    );
    element("engineering-submit-btn").disabled = !canSubmit;
  }

  function showStep(step, { focusHeading = true } = {}) {
    state.step = Math.max(1, Math.min(5, step));
    document.querySelectorAll("[data-wizard-page]").forEach((page) => {
      page.hidden = Number(page.dataset.wizardPage) !== state.step;
    });
    element("engineering-task-stepper").querySelectorAll("li").forEach((item) => {
      const itemStep = Number(item.dataset.step);
      if (itemStep === state.step) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
      item.classList.toggle("is-complete", itemStep < state.step);
    });
    element("engineering-previous-btn").hidden = state.step === 1;
    element("engineering-next-btn").hidden = state.step === 5;
    element("engineering-submit-btn").hidden = state.step !== 5;
    updatePreview();
    const activeStepperItem = element("engineering-task-stepper").querySelector(
      `[data-step="${state.step}"]`
    );
    if (activeStepperItem) {
      activeStepperItem.scrollIntoView({ behavior: "auto", block: "nearest", inline: "center" });
    }
    if (focusHeading) {
      const heading = document.querySelector(
        `[data-wizard-page="${state.step}"] h3[tabindex="-1"]`
      );
      if (heading) window.requestAnimationFrame(() => heading.focus());
    }
  }

  async function refreshRunnerStatus(openSerial) {
    state.runnerConnected = false;
    state.runnerStatus = null;
    renderRunnerState({ loading: true });
    try {
      const status = await api("/codex-runner/status");
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      state.runnerConnected = true;
      state.runnerStatus = status;
      codexRunnerStatusCache = status;
      renderRunnerState();
    } catch (error) {
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      state.runnerConnected = false;
      state.runnerStatus = null;
      renderRunnerState();
    }
  }

  async function loadEngineeringCapabilities(openSerial) {
    state.backendCapabilitiesLoaded = false;
    state.backendCapabilities = null;
    updatePreview();
    try {
      const capabilities = await api("/engineering-tasks/capabilities");
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      state.backendCapabilities = capabilities || { enabled: false };
    } catch (error) {
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      // Older/rolled-back servers do not expose the capability endpoint; keep
      // the exact legacy request contract instead of inventing immutable support.
      state.backendCapabilities = { enabled: false, compatibility_fallback: true };
    }
    state.backendCapabilitiesLoaded = true;
    applyBackendMode();
  }

  function populateCodingAgentProviders() {
    const field = element("engineering-agent-provider-field");
    const select = element("engineering-agent-provider");
    const providers = state.codingAgentProviders;
    select.replaceChildren();
    if (!providers.length) {
      // Discovery failed or returned nothing selectable: keep the exact
      // legacy default (codex) instead of blocking submission on this list.
      field.hidden = true;
      return;
    }
    providers.forEach((provider) => {
      select.append(new Option(provider.display_name || provider.provider_id, provider.provider_id));
    });
    select.value = providers.some((provider) => provider.provider_id === "codex")
      ? "codex"
      : providers[0].provider_id;
    // Only one reviewed+enabled provider (the default codex-only slice): no
    // decision for the operator to make, so the selector stays hidden.
    field.hidden = providers.length < 2;
  }

  async function loadCodingAgentProviders(openSerial) {
    state.codingAgentProviders = [];
    try {
      const response = await api("/coding-agents");
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      const providers = Array.isArray(response && response.providers) ? response.providers : [];
      // GET /coding-agents also carries not-yet-wired experimental adapters
      // (D1, CONTROLLED_CODING_RUNNER_V1); only a provider that can actually
      // start a turn belongs in this selection list.
      state.codingAgentProviders = providers.filter(
        (provider) => provider && provider.operations && provider.operations.start_turn === true
      );
    } catch (error) {
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      state.codingAgentProviders = [];
    }
    populateCodingAgentProviders();
  }

  function versionOptionLabel(version) {
    const commit = String(version.git_commit || "unknown").slice(0, 12);
    const ref = version.git_ref || "no ref";
    const created = version.created_at ? ` · ${version.created_at}` : "";
    return `${ref}@${commit}${created}`;
  }

  async function loadProjectVersions(projectName, openSerial) {
    const select = element("engineering-version-reference");
    select.disabled = true;
    select.replaceChildren(new Option("載入版本紀錄中…", ""));
    try {
      const versions = await api(`/projects/${encodeURIComponent(projectName)}/versions`);
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      state.versions = Array.isArray(versions) ? versions : [];
      select.replaceChildren();
      if (!state.versions.length) {
        select.append(new Option("尚無 ProjectVersion 紀錄", ""));
        select.disabled = true;
      } else {
        state.versions.forEach((version) => {
          select.append(new Option(versionOptionLabel(version), version.id));
        });
        select.disabled = false;
        select.value = state.versions[0].id;
      }
      updatePreview();
    } catch (error) {
      if (openSerial !== state.openSerial || element("engineering-task-dialog").hidden) return;
      state.versions = [];
      select.replaceChildren(new Option("版本紀錄無法載入（不影響相容送出）", ""));
      select.disabled = true;
      updatePreview();
    }
  }

  function populateValidationTargets() {
    const select = element("engineering-validation-target");
    const configs = typeof serverConfigsCache === "undefined" ? [] : serverConfigsCache;
    select.replaceChildren(new Option("選擇已啟用的 worker", ""));
    configs
      .filter((server) => server.enabled)
      .forEach((server) => select.append(new Option(server.name, server.name)));
    select.value = "";
    select.disabled = true;
  }

  function resetWizard(projectName) {
    element("engineering-task-form").reset();
    element("engineering-modify-files").checked = true;
    element("engineering-project-name").textContent = projectName;
    element("engineering-preview-project").textContent = projectName;
    element("engineering-version-reference").replaceChildren(
      new Option("載入版本紀錄中…", "")
    );
    element("engineering-version-reference").disabled = true;
    populateValidationTargets();
    clearFieldErrors();
    state.runnerStatus = null;
    state.runnerConnected = false;
    state.versions = [];
    state.backendCapabilities = null;
    state.backendCapabilitiesLoaded = false;
    state.codingAgentProviders = [];
    element("engineering-agent-provider-field").hidden = true;
    element("engineering-agent-provider").replaceChildren();
    state.submitting = false;
    applyBackendMode();
  }

  function openEngineeringTaskWizard(projectName) {
    if (!flags.AI_ENGINEERING_TASK_UI_V2) {
      if (state.legacyOpenCodingTaskModal) state.legacyOpenCodingTaskModal(projectName);
      return;
    }
    if (!element("engineering-task-detail-dialog").hidden) {
      closeEngineeringTaskDetail({ restoreFocus: false });
    }
    const dialog = element("engineering-task-dialog");
    state.opener = document.activeElement;
    state.targetProject = projectName;
    const openSerial = ++state.openSerial;
    resetWizard(projectName);
    dialog.hidden = false;
    element("modal-backdrop").classList.add("open");
    document.body.classList.add("dialog-open");
    showStep(1, { focusHeading: false });
    window.requestAnimationFrame(() => element("engineering-task-title").focus());
    refreshRunnerStatus(openSerial);
    loadEngineeringCapabilities(openSerial);
    loadCodingAgentProviders(openSerial);
    loadProjectVersions(projectName, openSerial);
  }

  function closeEngineeringTaskWizard({ restoreFocus = true } = {}) {
    const dialog = element("engineering-task-dialog");
    if (!dialog || dialog.hidden) return;
    ++state.openSerial;
    dialog.hidden = true;
    document.body.classList.remove("dialog-open");
    if (!document.querySelector(".modal.open")) {
      element("modal-backdrop").classList.remove("open");
    }
    const opener = state.opener;
    state.opener = null;
    state.targetProject = null;
    setGlobalError("");
    if (restoreFocus && opener && typeof opener.focus === "function") {
      window.requestAnimationFrame(() => opener.focus());
    }
  }

  async function submitEngineeringTask(event) {
    event.preventDefault();
    if (state.step !== 5) {
      if (validateStep(state.step)) showStep(state.step + 1);
      return;
    }
    if (!state.targetProject || !validateAll()) return;

    const values = readFormValues();
    const instruction = renderStructuredInstruction(values, {
      enforceFinalGitPaths: finalGitPathPolicyEnabled(),
    });
    const baseBranch = values.baseBranch || null;
    const validationTarget = values.workerValidation ? values.validationTarget || null : null;
    const immutable = immutableBackendEnabled();
    let endpoint;
    let body;
    if (immutable) {
      const version = selectedVersion();
      endpoint = `/projects/${encodeURIComponent(state.targetProject)}/engineering-tasks/request`;
      body = {
        project_version_id: version.id,
        agent_provider_id: element("engineering-agent-provider").value || "codex",
        objective: values.objective,
        background: values.background || null,
        expected_changes: bulletItems(values.expectedChanges),
        non_goals: bulletItems(values.nonGoals),
        allowed_paths: finalGitPathPolicyEnabled()
          ? canonicalPathItems(values.allowedPaths)
          : bulletItems(values.allowedPaths),
        prohibited_changes: bulletItems(values.prohibitedChanges),
        acceptance_criteria: bulletItems(values.acceptanceCriteria),
        validation: {
          tests_lint: values.runTests,
          build_smoke: values.runBuild,
          continue_fixing_failures: values.autoFix,
          worker_validation_target: validationTarget,
        },
        execution_permissions: {
          modify_project_files: true,
          install_dependencies: false,
          external_network: false,
          environment_references: [],
          secret_references: [],
        },
      };
      if (finalGitPathPolicyEnabled()) {
        body.prohibited_paths = canonicalPathItems(values.prohibitedPaths);
      }
    } else {
      // Exact rollback contract for servers with the immutable backend disabled.
      endpoint = `/projects/${encodeURIComponent(state.targetProject)}/coding-task-request`;
      body = { instruction, base_branch: baseBranch, validation_target: validationTarget };
    }

    state.submitting = true;
    updatePreview();
    try {
      await api(endpoint, {
        method: "POST",
        body: JSON.stringify(body),
      });
      showToast(
        immutable
          ? "已建立 immutable AI 工程任務核准請求（需人工核准）"
          : "已建立相容版 AI 工程任務核准請求（需人工核准）"
      );
      closeEngineeringTaskWizard();
      refreshAll();
    } catch (error) {
      state.submitting = false;
      updatePreview();
      setGlobalError(`建立失敗：${error.message}`);
      element("engineering-task-global-error").focus();
    }
  }

  function focusableElements(container) {
    return Array.from(
      container.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((item) => !item.hidden && item.offsetParent !== null);
  }

  function isolateTaskDetailBackground() {
    if (state.detailBackgroundSnapshot !== null) return;
    const dialog = element("engineering-task-detail-dialog");
    const backdrop = element("modal-backdrop");
    state.detailBackgroundSnapshot = Array.from(document.body.children)
      .filter((candidate) => (
        candidate instanceof HTMLElement
        && candidate !== dialog
        && candidate !== backdrop
        && !candidate.matches("script, style")
      ))
      .map((candidate) => {
        const snapshot = {
          element: candidate,
          inert: candidate.inert,
          hadAriaHidden: candidate.hasAttribute("aria-hidden"),
          ariaHidden: candidate.getAttribute("aria-hidden"),
        };
        candidate.inert = true;
        candidate.setAttribute("aria-hidden", "true");
        return snapshot;
      });
  }

  function restoreTaskDetailBackground() {
    const snapshots = state.detailBackgroundSnapshot;
    state.detailBackgroundSnapshot = null;
    if (!Array.isArray(snapshots)) return;
    snapshots.forEach((snapshot) => {
      if (!snapshot.element.isConnected) return;
      snapshot.element.inert = snapshot.inert;
      if (snapshot.hadAriaHidden) {
        snapshot.element.setAttribute("aria-hidden", snapshot.ariaHidden);
      } else {
        snapshot.element.removeAttribute("aria-hidden");
      }
    });
  }

  function handleDialogKeydown(event) {
    const dialog = element("engineering-task-dialog");
    if (dialog.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeEngineeringTaskWizard();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = focusableElements(dialog);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openNavigation() {
    document.body.classList.add("navigation-open");
    element("navigation-scrim").hidden = false;
    element("mobile-nav-open-btn").setAttribute("aria-expanded", "true");
    syncNavigationAccessibility();
    element("mobile-nav-close-btn").focus();
  }

  function closeNavigation({ restoreFocus = false } = {}) {
    const wasOpen = document.body.classList.contains("navigation-open");
    const navigation = element("primary-navigation");
    const focusWasInside = navigation.contains(document.activeElement);
    document.body.classList.remove("navigation-open");
    element("navigation-scrim").hidden = true;
    element("mobile-nav-open-btn").setAttribute("aria-expanded", "false");
    const frame = document.querySelector(".app-frame");
    frame.inert = false;
    frame.removeAttribute("aria-hidden");
    if (wasOpen && (restoreFocus || focusWasInside)) {
      element("mobile-nav-open-btn").focus();
    }
    syncNavigationAccessibility();
  }

  function mobileNavigationActive() {
    return window.matchMedia("(max-width: 768px)").matches;
  }

  function syncNavigationAccessibility() {
    const navigation = element("primary-navigation");
    const frame = document.querySelector(".app-frame");
    const mobile = mobileNavigationActive();
    const open = mobile && document.body.classList.contains("navigation-open");
    navigation.inert = mobile && !open;
    if (mobile && !open) navigation.setAttribute("aria-hidden", "true");
    else navigation.removeAttribute("aria-hidden");
    frame.inert = open;
    if (open) frame.setAttribute("aria-hidden", "true");
    else frame.removeAttribute("aria-hidden");
  }

  function trapNavigationFocus(event) {
    if (
      event.key !== "Tab" ||
      !mobileNavigationActive() ||
      !document.body.classList.contains("navigation-open")
    ) return;
    const navigation = element("primary-navigation");
    const focusable = Array.from(
      navigation.querySelectorAll(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'
      )
    ).filter((control) => !control.hidden);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function updatePageContext() {
    const hash = window.location.hash || "";
    let title = "總覽";
    let breadcrumb = "平台 / 總覽";
    const projectRoute = parseProjectWorkspaceRoute(hash);
    if (projectRoute) {
      title = projectRoute.name;
      const sectionLabel = PROJECT_WORKSPACE_SECTION_LABELS[projectRoute.section];
      breadcrumb = `平台 / 專案 / ${projectRoute.name} / ${sectionLabel}`;
    } else if (hash.startsWith("#tab/")) {
      const tab = hash.slice("#tab/".length);
      title = TAB_LABELS[tab] || "總覽";
      breadcrumb = `平台 / ${title}`;
    }
    element("page-title").textContent = title;
    element("page-breadcrumb").textContent = breadcrumb;
  }

  function updateIdentity(authInfo) {
    state.currentAuthInfo = authInfo || null;
    const badge = element("current-role-badge");
    if (!badge) return;
    const actor = authInfo && authInfo.actor;
    if (!actor) {
      badge.hidden = true;
      badge.textContent = "";
      if (state.currentProject) renderProjectWorkspaceRole(state.currentProject);
      return;
    }
    let label = "Authenticated actor";
    if (actor.type === "service") {
      label = "Service actor";
    } else if (actor.platform_admin) {
      label = "Platform Admin";
    } else {
      const memberships = Array.isArray(authInfo.project_memberships)
        ? authInfo.project_memberships
        : [];
      const highestRole = memberships
        .map((membership) => membership.role)
        .filter((role) => Object.prototype.hasOwnProperty.call(ROLE_PRIORITY, role))
        .sort((a, b) => ROLE_PRIORITY[b] - ROLE_PRIORITY[a])[0];
      label = highestRole ? ROLE_LABELS[highestRole] : "No project role";
      if (memberships.length > 1 && highestRole) label += ` · ${memberships.length} projects`;
    }
    badge.textContent = label;
    badge.hidden = false;
    if (state.currentProject) renderProjectWorkspaceRole(state.currentProject);
    if (!element("engineering-task-dialog").hidden) renderRunnerState();
  }

  function engineeringTaskDetailEnabled() {
    return Boolean(flags.AI_ENGINEERING_TASK_DETAIL_UI_V1);
  }

  function safeStatusKey(value) {
    const raw = String(value || "unknown").toLowerCase();
    if (Object.prototype.hasOwnProperty.call(ENGINEERING_TASK_STATUS, raw)) return raw;
    return {
      approved: "queued",
      preparing: "staging",
      succeeded: "done",
      completed: "done",
      offline: "disconnected",
      unreachable: "disconnected",
    }[raw] || "unknown";
  }

  function presentationValue(value, fallback = "-") {
    if (value == null || value === "") return fallback;
    if (typeof value === "object") {
      return String(value.label || value.summary || value.value || value.status || fallback);
    }
    return String(value);
  }

  function presentationLabel(value) {
    if (!value || typeof value !== "object") return "";
    const label = value.label || value.summary;
    return label ? String(label) : "";
  }

  function identityDisplay(value, fallback = "-") {
    if (!value) return fallback;
    if (typeof value === "string") return value;
    if (typeof value !== "object") return fallback;
    return String(value.display_name || value.label || value.actor_type || fallback);
  }

  function taskPresentation(task) {
    return task && task.presentation && typeof task.presentation === "object"
      ? task.presentation
      : {};
  }

  function taskStatus(task) {
    const presentation = taskPresentation(task);
    const stateValue = presentation.state;
    if (stateValue && typeof stateValue === "object") {
      const candidates = [
        stateValue.code,
        stateValue.key,
        stateValue.value,
        stateValue.status,
        stateValue.tone,
      ];
      for (const candidate of candidates) {
        if (!candidate) continue;
        const normalized = safeStatusKey(candidate);
        if (normalized !== "unknown" || String(candidate).toLowerCase() === "unknown") {
          return normalized;
        }
      }
    }
    return safeStatusKey(
      (typeof stateValue === "string" && stateValue) || (task && task.status)
    );
  }

  function statusCopy(status) {
    return ENGINEERING_TASK_STATUS[safeStatusKey(status)];
  }

  function setStatusBadge(badge, status, serverLabel = "") {
    const key = safeStatusKey(status);
    const copy = statusCopy(key);
    const tone = key === "path_policy_violation" ? "failed" : key;
    const fixedPolicyLabel = [
      "pending_approval", "path_policy_violation", "secret_violation",
    ].includes(key);
    badge.className = `status-pill status-${tone} status-${key}`;
    badge.textContent = fixedPolicyLabel ? copy.label : (serverLabel || copy.label);
  }

  function makeElement(tagName, className, textValue) {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (textValue != null) node.textContent = String(textValue);
    return node;
  }

  function makeRetryControl(kind, label, retry) {
    const loading = kind === "loading";
    const button = makeElement(
      "button",
      "component-state-retry",
      loading ? "載入中…" : label
    );
    button.type = "button";
    button.disabled = loading;
    if (!loading && typeof retry === "function") {
      button.addEventListener("click", retry);
    }
    return button;
  }

  function appendLabeledCell(row, label, value, className = "") {
    const cell = document.createElement("td");
    cell.dataset.label = label;
    if (className) cell.className = className;
    cell.textContent = value == null || value === "" ? "-" : String(value);
    row.append(cell);
    return cell;
  }

  function setEngineeringListState(kind, title, message, { hidden = false } = {}) {
    const container = element("engineering-task-list-state");
    container.hidden = hidden;
    if (hidden) return;
    container.className = `component-state state-${kind}`;
    const icon = makeElement("div", "component-state-icon", {
      loading: "◌",
      empty: "∅",
      error: "×",
      disconnected: "↯",
    }[kind] || "?");
    icon.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div");
    copy.append(makeElement("strong", "", title), makeElement("p", "", message));
    const controls = [];
    if (["loading", "error", "disconnected"].includes(kind)) {
      controls.push(makeRetryControl(kind, "重試載入任務", loadEngineeringTasks));
    }
    container.replaceChildren(icon, copy, ...controls);
  }

  function engineeringListHeader() {
    const row = element("engineering-task-table-head");
    const labels = ["Task ID", "專案", "狀態 / 階段", "Immutable base", "更新時間"];
    row.replaceChildren(...labels.map((label) => {
      const heading = makeElement("th", "", label);
      heading.scope = "col";
      return heading;
    }));
  }

  function renderEngineeringTasks(tasks) {
    if (!engineeringTaskDetailEnabled()) return false;
    const rows = Array.isArray(tasks) ? tasks : [];
    state.engineeringTasks = rows;
    element("engineering-task-list-mode").textContent = "Task workflow";
    engineeringListHeader();
    const tbody = element("coding-runs-tbody");
    element("engineering-task-table").classList.add("task-centric");
    tbody.replaceChildren();
    element("engineering-task-table").hidden = false;
    if (!rows.length) {
      const row = document.createElement("tr");
      const cell = makeElement("td", "muted", "目前沒有 AI 工程任務");
      cell.colSpan = 5;
      cell.dataset.label = "狀態";
      row.append(cell);
      tbody.append(row);
      setEngineeringListState("empty", "尚無 AI 工程任務", "從專案工作區建立任務後，會在這裡顯示核准與執行進度。");
      return true;
    }
    setEngineeringListState("loading", "", "", { hidden: true });
    rows.forEach((task) => {
      const row = document.createElement("tr");
      row.className = "engineering-task-row";
      const idCell = document.createElement("td");
      idCell.dataset.label = "Task ID";
      const openButton = makeElement("button", "engineering-task-open", task.id || "unknown");
      openButton.type = "button";
      openButton.dataset.engineeringTaskOpen = "true";
      openButton.dataset.taskId = String(task.id || "");
      openButton.setAttribute("aria-label", `開啟 AI 工程任務 ${String(task.id || "unknown")} 詳情`);
      idCell.append(openButton);
      row.append(idCell);
      appendLabeledCell(row, "專案", task.project || "-");

      const statusCell = document.createElement("td");
      statusCell.dataset.label = "狀態 / 階段";
      const badge = document.createElement("span");
      setStatusBadge(badge, taskStatus(task), presentationLabel(taskPresentation(task).state));
      statusCell.append(badge);
      const phase = taskPresentation(task).phase;
      if (phase) statusCell.append(makeElement("span", "task-metadata", presentationValue(phase)));
      row.append(statusCell);

      const base = task.exact_base_commit || task.base_commit || task.project_version_commit || "未回傳";
      appendLabeledCell(row, "Immutable base", base, "engineering-task-base");
      appendLabeledCell(row, "更新時間", task.updated_at || task.finished_at || task.created_at || "-");
      tbody.append(row);
    });
    return true;
  }

  function isConnectionError(error) {
    return /network|fetch|connection|disconnected|offline/i.test(String(error && error.message));
  }

  async function loadEngineeringTasks() {
    if (!engineeringTaskDetailEnabled()) return false;
    const serial = ++state.listLoadSerial;
    engineeringListHeader();
    element("engineering-task-list-mode").textContent = "Task workflow";
    element("engineering-task-table").hidden = true;
    setEngineeringListState("loading", "正在載入 AI 工程任務", "取得 immutable 任務與 legacy adapter 紀錄。");
    try {
      const tasks = await api("/engineering-tasks");
      if (serial !== state.listLoadSerial) return true;
      renderEngineeringTasks(tasks);
    } catch (error) {
      if (serial !== state.listLoadSerial) return true;
      const disconnected = isConnectionError(error);
      setEngineeringListState(
        disconnected ? "disconnected" : "error",
        disconnected ? "與平台連線中斷" : "AI 工程任務載入失敗",
        disconnected
          ? "連線恢復後請重新整理；disconnected 不會被視為任務失敗。"
          : String(error.message || error)
      );
      element("engineering-task-table").hidden = true;
    }
    return true;
  }

  function clearEngineeringTasks() {
    ++state.listLoadSerial;
    state.engineeringTasks = [];
    if (!element("engineering-task-detail-dialog").hidden) {
      closeEngineeringTaskDetail({ restoreFocus: false });
    }
    if (engineeringTaskDetailEnabled()) renderEngineeringTasks([]);
  }

  function setTaskDetailState(kind, title, message) {
    const container = element("engineering-task-detail-state");
    container.hidden = false;
    container.className = `component-state state-${kind}`;
    const icon = makeElement("div", "component-state-icon", {
      loading: "◌",
      error: "×",
      disconnected: "↯",
    }[kind] || "?");
    icon.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div");
    copy.append(makeElement("strong", "", title), makeElement("p", "", message));
    const controls = [];
    if (["loading", "error", "disconnected"].includes(kind)) {
      controls.push(makeRetryControl(kind, "重試載入詳情", retryEngineeringTaskDetail));
    }
    container.replaceChildren(icon, copy, ...controls);
    element("engineering-task-detail-content").hidden = true;
    disableTaskDetailActions();
    element("engineering-task-detail-actions").hidden = true;
  }

  function disableTaskDetailActions() {
    for (const id of (
      [
        "engineering-task-validation-action",
        "engineering-task-cleanup-action",
        "engineering-task-download-patch-action",
        "engineering-task-retry-action",
        "engineering-task-discard-action",
        "engineering-task-promote-action",
      ]
    )) {
      const button = element(id);
      if (!button) continue;
      button.disabled = true;
      button.removeAttribute("aria-busy");
      delete button.dataset.codingRunId;
      delete button.dataset.engineeringTaskId;
      delete button.dataset.requestMode;
      delete button.dataset.downloadUrl;
      delete button.dataset.taskId;
    }
    document.querySelectorAll("[data-future-task-action]").forEach((button) => {
      button.disabled = true;
    });
  }

  function detailHeading(panel, title) {
    panel.append(makeElement("h3", "", title));
  }

  function appendDetailCard(grid, label, value, { code = false } = {}) {
    const card = makeElement("div", "task-detail-card");
    card.append(makeElement("span", "", label));
    card.append(makeElement(code ? "code" : "strong", "", presentationValue(value)));
    grid.append(card);
  }

  function emptyDetailState(panel, message) {
    panel.append(makeElement("div", "task-empty-state", message));
  }

  function taskObjective(task) {
    const structured = task.structured_request || task.request || {};
    return task.objective || structured.objective || task.instruction || "未提供任務目標";
  }

  function taskLatestCheckpoint(task) {
    if (task.latest_checkpoint || task.result_commit) {
      return task.latest_checkpoint || task.result_commit;
    }
    if (task.coding_run && task.coding_run.result_commit) {
      return task.coding_run.result_commit;
    }
    const attempts = Array.isArray(task.attempts) ? task.attempts : [];
    const completed = attempts.slice().reverse().find((attempt) => attempt.result_commit);
    return completed ? completed.result_commit : "尚無";
  }

  function renderTaskAttempts(task, panel) {
    const section = makeElement("section", "task-detail-section");
    section.append(makeElement("h4", "", "執行嘗試"));
    const attempts = Array.isArray(task.attempts) ? task.attempts : [];
    if (!attempts.length) {
      emptyDetailState(section, "尚無執行 attempt；缺少紀錄不會被推斷為失敗。");
      panel.append(section);
      return;
    }
    const list = makeElement("div", "task-test-list");
    attempts.forEach((attempt) => {
      const card = makeElement("article", "task-test");
      const attemptState = attempt.state && typeof attempt.state === "object"
        ? attempt.state
        : {};
      const statusValue = attemptState.code || attemptState.status
        || (typeof attempt.state === "string" ? attempt.state : null)
        || attempt.result_status
        || "unknown";
      const badge = document.createElement("span");
      setStatusBadge(badge, statusValue, presentationLabel(attempt.state));
      const number = attempt.attempt_number == null
        ? "Legacy attempt"
        : `Attempt ${attempt.attempt_number}`;
      card.append(badge, makeElement("strong", "", number));
      const metadata = [
        `Phase ${presentationValue(attempt.phase, "unknown")}`,
        `Health ${presentationValue(attempt.health, "unknown")}`,
        attempt.result_status && `Result ${attempt.result_status}`,
        attempt.created_at && `created ${attempt.created_at}`,
        attempt.started_at && `started ${attempt.started_at}`,
        attempt.finished_at && `finished ${attempt.finished_at}`,
      ].filter(Boolean).join(" · ");
      if (metadata) card.append(makeElement("span", "task-metadata", metadata));
      if (attempt.result_commit) {
        const commit = makeElement("code", "", attempt.result_commit);
        commit.setAttribute("aria-label", "Result commit");
        card.append(commit);
      }
      list.append(card);
    });
    section.append(list);
    panel.append(section);
  }

  function renderTaskFinalResponse(task, panel) {
    const section = makeElement("section", "task-detail-section");
    section.append(makeElement("h4", "", "代理最終回覆"));
    const response = task.final_response && typeof task.final_response === "object"
      ? task.final_response
      : null;
    if (!response || (!response.available && !response.withheld)) {
      emptyDetailState(section, "尚無可顯示的最終回覆。");
      panel.append(section);
      return;
    }
    if (response.withheld) {
      const alert = makeElement("div", "ui-alert ui-alert-warning");
      alert.setAttribute("role", "note");
      alert.append(
        makeElement("span", "", "!"),
        makeElement("div", "", "最終回覆因安全檢查而整段隱藏；瀏覽器不會顯示原始內容。")
      );
      section.append(alert);
      panel.append(section);
      return;
    }
    if (response.available !== true || typeof response.content !== "string") {
      const error = makeElement("div", "component-state state-error");
      error.setAttribute("role", "status");
      error.append(
        makeElement("div", "component-state-icon", "×"),
        makeElement("strong", "", "最終回覆 metadata 不完整")
      );
      section.append(error);
      panel.append(section);
      return;
    }
    const output = makeElement("pre", "task-diff-viewer task-final-response", response.content);
    output.tabIndex = 0;
    section.append(output);
    const metadata = [
      response.redacted && "已由伺服器遮罩",
      response.truncated && "內容已截斷",
    ].filter(Boolean).join(" · ");
    if (metadata) section.append(makeElement("span", "task-metadata", metadata));
    panel.append(section);
  }

  function renderTaskOverview(task) {
    const panel = element("engineering-task-pane-overview");
    panel.replaceChildren();
    detailHeading(panel, "任務總覽");
    const objective = makeElement("section", "task-detail-card");
    objective.append(makeElement("span", "", "Task objective"), makeElement("strong", "", taskObjective(task)));
    panel.append(objective);
    const presentation = taskPresentation(task);
    const grid = makeElement("div", "task-detail-grid");
    appendDetailCard(grid, "Project", task.project);
    appendDetailCard(grid, "Agent", task.agent_provider_id || task.agent || "Codex");
    appendDetailCard(grid, "Task ID", task.id, { code: true });
    appendDetailCard(grid, "ProjectVersion", task.project_version_id || "legacy / 未綁定", { code: true });
    appendDetailCard(grid, "Immutable base", task.exact_base_commit || task.base_commit || "未綁定", { code: true });
    appendDetailCard(grid, "Latest checkpoint", taskLatestCheckpoint(task), { code: true });
    appendDetailCard(grid, "Phase", presentation.phase || task.phase || "unknown");
    appendDetailCard(grid, "Execution health", presentation.execution_health || "unknown");
    appendDetailCard(grid, "Runner connection", presentation.runner_connection || "unknown");
    appendDetailCard(
      grid,
      "Requester",
      task.requester_display || identityDisplay(task.requester || task.requester_identity || task.requested_by)
    );
    appendDetailCard(
      grid,
      "Approver",
      task.approver_display || identityDisplay(task.approver || task.approver_identity, "尚未決定")
    );
    appendDetailCard(grid, "Created", task.created_at);
    appendDetailCard(grid, "Updated", task.updated_at || task.finished_at);
    panel.append(grid);
    renderTaskAttempts(task, panel);
    renderTaskFinalResponse(task, panel);
  }

  function numericTaskEventId(event) {
    const value = Number(event && event.id);
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  }

  function resetTaskEventPaging(events) {
    const ids = events.map(numericTaskEventId).filter((value) => value !== null);
    state.detailEventCursor = ids.length ? Math.max(...ids) : null;
    state.detailEventsCanLoadMore = (
      events.length === TASK_EVENT_PAGE_SIZE && state.detailEventCursor !== null
    );
    state.detailEventsLoading = false;
    state.detailEventsNotice = null;
  }

  function appendTaskEventPagination(panel, eventCount) {
    const container = makeElement("div", "task-event-pagination");
    container.id = "engineering-task-events-pagination";
    container.setAttribute("role", "status");
    container.setAttribute("aria-live", "polite");
    const message = makeElement(
      "span",
      "task-metadata",
      "目前顯示 " + String(eventCount) + " 筆事件。"
        + (state.detailEventsNotice ? ` ${state.detailEventsNotice}` : "")
    );
    message.id = "engineering-task-events-pagination-message";
    container.append(message);
    if (state.detailEventsCanLoadMore) {
      const button = makeElement("button", "", "載入更多事件");
      button.id = "engineering-task-events-load-more";
      button.type = "button";
      button.setAttribute("aria-describedby", message.id);
      button.addEventListener("click", loadMoreTaskEvents);
      container.append(button);
    }
    panel.append(container);
  }

  function renderTaskTimeline(task, { resetPaging = true } = {}) {
    const panel = element("engineering-task-pane-timeline");
    panel.replaceChildren();
    detailHeading(panel, "進度時間軸");
    const events = Array.isArray(task.events) ? task.events : [];
    if (resetPaging) resetTaskEventPaging(events);
    if (!events.length) {
      emptyDetailState(panel, "尚無可顯示的 task event；缺少 event 不會被推斷為失敗。");
      return;
    }
    const list = makeElement("ol", "task-event-list");
    events.forEach((event) => {
      const item = makeElement("li", "task-event");
      item.append(makeElement("strong", "", event.summary || event.label || event.kind || "Task event"));
      item.append(makeElement(
        "time",
        "",
        event.occurred_at || event.recorded_at || event.created_at || event.timestamp || "時間未知"
      ));
      const detail = event.message || event.detail || event.description;
      if (detail) item.append(makeElement("p", "", detail));
      if (event.attempt_number != null) item.append(makeElement("span", "task-metadata", `Attempt ${event.attempt_number}`));
      list.append(item);
    });
    panel.append(list);
    appendTaskEventPagination(panel, events.length);
  }

  async function loadMoreTaskEvents() {
    if (
      state.detailEventsLoading
      || !state.detailEventsCanLoadMore
      || state.detailEventCursor === null
      || !state.detailTaskId
      || !state.detailTask
    ) return;
    const taskId = state.detailTaskId;
    const task = state.detailTask;
    const cursor = state.detailEventCursor;
    const serial = state.detailOpenSerial;
    const container = element("engineering-task-events-pagination");
    const button = element("engineering-task-events-load-more");
    const message = element("engineering-task-events-pagination-message");
    if (!container || !button || !message) return;
    state.detailEventsLoading = true;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "載入中…";
    message.className = "task-metadata";
    message.textContent = "正在載入下一批事件。";
    const endpoint = `/engineering-tasks/${encodeURIComponent(taskId)}`
      + `/events?after_id=${encodeURIComponent(String(cursor))}&limit=${TASK_EVENT_PAGE_SIZE}`;
    try {
      const page = await api(endpoint);
      if (
        serial !== state.detailOpenSerial
        || state.detailTaskId !== taskId
        || state.detailTask !== task
      ) return;
      if (!Array.isArray(page)) throw new Error("事件回應格式無效");
      const current = Array.isArray(task.events) ? task.events : [];
      const seenIds = new Set(
        current.map(numericTaskEventId).filter((value) => value !== null)
      );
      const additions = page.filter((event) => {
        const id = numericTaskEventId(event);
        if (id === null || id <= cursor || seenIds.has(id)) return false;
        seenIds.add(id);
        return true;
      });
      const nextIds = additions.map(numericTaskEventId);
      const nextCursor = nextIds.length ? Math.max(...nextIds) : cursor;
      if (!page.length) {
        state.detailEventsCanLoadMore = false;
        state.detailEventsNotice = null;
        renderTaskTimeline(task, { resetPaging: false });
        return;
      }
      if (!additions.length || nextCursor <= cursor) {
        state.detailEventsCanLoadMore = false;
        state.detailEventsNotice = "事件游標未前進，已停止載入以避免重複迴圈。";
        renderTaskTimeline(task, { resetPaging: false });
        return;
      }
      task.events = [...current, ...additions];
      state.detailEventCursor = nextCursor;
      state.detailEventsCanLoadMore = page.length === TASK_EVENT_PAGE_SIZE;
      state.detailEventsNotice = null;
      renderTaskTimeline(task, { resetPaging: false });
    } catch (error) {
      if (
        serial !== state.detailOpenSerial
        || state.detailTaskId !== taskId
        || state.detailTask !== task
      ) return;
      const disconnected = isConnectionError(error);
      message.className = `field-error state-${disconnected ? "disconnected" : "error"}`;
      message.textContent = disconnected
        ? "事件連線中斷；這不代表任務失敗。"
        : "事件載入失敗；既有事件仍保留，請重試。";
      button.textContent = "重試載入更多事件";
      button.disabled = false;
    } finally {
      if (
        serial === state.detailOpenSerial
        && state.detailTaskId === taskId
        && state.detailTask === task
      ) {
        state.detailEventsLoading = false;
        const currentButton = element("engineering-task-events-load-more");
        if (currentButton) currentButton.removeAttribute("aria-busy");
      }
    }
  }

  function renderTaskCommands(task) {
    const panel = element("engineering-task-pane-commands");
    panel.replaceChildren();
    detailHeading(panel, "命令與經過遮罩的日誌");
    const commands = Array.isArray(task.commands) ? task.commands : [];
    if (!commands.length) {
      emptyDetailState(panel, "尚無命令記錄。任務尚未執行與伺服器無法確認是不同狀態。");
      return;
    }
    const list = makeElement("div", "task-command-list");
    commands.forEach((command) => {
      const card = makeElement("article", "task-command");
      const status = command.status || "unknown";
      const badge = document.createElement("span");
      setStatusBadge(badge, status);
      card.append(badge);
      card.append(makeElement("strong", "", command.title || command.policy_family || "Command"));
      const meta = [
        command.execution_location_label || "執行位置未公開",
        command.working_directory_label || "工作目錄未公開",
        command.started_at && `start ${command.started_at}`,
        command.finished_at && `end ${command.finished_at}`,
        command.exit_code != null && `exit ${command.exit_code}`,
        (command.duration_seconds != null || command.duration != null)
          && `${command.duration_seconds != null ? command.duration_seconds : command.duration}s`,
      ].filter(Boolean).join(" · ");
      if (meta) card.append(makeElement("span", "task-command-meta", meta));
      card.append(makeElement("code", "", command.display_command || "命令內容未提供"));
      const output = typeof command.redacted_output === "string"
        ? command.redacted_output
        : null;
      if (output) card.append(makeElement("pre", "task-diff-viewer", output));
      const approvalInfo = command.approval || {};
      const approvalId = command.approval_id || approvalInfo.approval_id;
      const approvalRequired = command.approval_required || approvalInfo.required;
      const approval = approvalId
        ? `Approval ${approvalId}`
        : (approvalRequired ? "需要獨立核准" : "未回傳獨立核准");
      card.append(makeElement("span", "task-command-meta", approval));
      const logInfo = command.log && typeof command.log === "object" ? command.log : {};
      if (logInfo.withheld === true) {
        card.append(makeElement(
          "p",
          "task-metadata",
          "命令日誌因安全政策而整段隱藏。"
        ));
      } else if (logInfo.available === true && /^\d+$/.test(String(command.id || ""))) {
        const logButton = makeElement("button", "", "查看經過遮罩的日誌");
        logButton.type = "button";
        logButton.addEventListener("click", async () => {
          const taskId = state.detailTaskId;
          const detailTask = state.detailTask;
          const detailSerial = state.detailOpenSerial;
          const isCurrent = () => (
            detailSerial === state.detailOpenSerial
            && state.detailTaskId === taskId
            && state.detailTask === detailTask
            && card.isConnected
          );
          if (!taskId || !detailTask) return;
          logButton.disabled = true;
          logButton.textContent = "載入中…";
          try {
            const response = await api(
              `/engineering-tasks/${encodeURIComponent(taskId)}`
              + `/commands/${encodeURIComponent(String(command.id))}/log`
            );
            if (!isCurrent()) return;
            const existing = card.querySelector("pre.task-command-log");
            if (existing) existing.remove();
            const content = response && response.withheld === true
              ? "日誌包含高風險敏感內容，已整段隱藏。"
              : (
                response
                && response.available === true
                && typeof response.content === "string"
                  ? response.content
                  : "日誌回應未包含可安全顯示的內容。"
              );
            const outputNode = makeElement("pre", "task-diff-viewer task-command-log", content);
            outputNode.tabIndex = 0;
            card.append(outputNode);
          } catch (error) {
            if (!isCurrent()) return;
            const alert = makeElement(
              "div",
              "ui-alert ui-alert-error",
              `日誌載入失敗：${String(error.message || error)}`
            );
            card.append(alert);
          } finally {
            if (isCurrent()) {
              logButton.disabled = false;
              logButton.textContent = "重新載入經過遮罩的日誌";
            }
          }
        });
        card.append(logButton);
      }
      list.append(card);
    });
    panel.append(list);
  }

  function renderTaskChanges(task) {
    const panel = element("engineering-task-pane-changes");
    panel.replaceChildren();
    detailHeading(panel, "變更與 Diff");
    const changes = task.changes && typeof task.changes === "object" ? task.changes : {};
    const summary = makeElement("div", "task-detail-card");
    summary.append(makeElement("span", "", "Change summary"));
    summary.append(makeElement("strong", "", changes.summary || "尚無變更摘要"));
    if (changes.truncated) summary.append(makeElement("p", "task-metadata", "差異內容已截斷。"));
    panel.append(summary);
    const diffState = makeElement(
      "div",
      "task-empty-state",
      changes.withheld === true
        ? "Diff 因安全政策而整段隱藏；瀏覽器不會發出內容請求。"
        : (
          changes.available === true
            ? "開啟此分頁後載入經後端遮罩的 diff。"
            : "伺服器未明確標示 diff 可用，內容維持隱藏。"
        )
    );
    diffState.id = "engineering-task-diff-state";
    panel.append(diffState);
  }

  function setTaskDiffState(kind, title, message, { focusRetry = false } = {}) {
    const target = element("engineering-task-diff-state");
    if (!target) return null;
    target.className = `component-state state-${kind}`;
    target.setAttribute("role", "status");
    target.setAttribute("aria-live", "polite");
    target.setAttribute("aria-atomic", "true");
    const icon = makeElement("div", "component-state-icon", {
      loading: "◌",
      error: "×",
      disconnected: "↯",
    }[kind] || "?");
    icon.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div");
    copy.append(makeElement("strong", "", title), makeElement("p", "", message));
    const retry = makeRetryControl(
      kind,
      "重試載入 Diff",
      () => loadTaskDiff({ focusState: true })
    );
    target.replaceChildren(icon, copy, retry);
    if (focusRetry && !retry.disabled) {
      window.requestAnimationFrame(() => retry.focus({ preventScroll: true }));
    }
    return target;
  }

  function normalizeTests(task) {
    if (Array.isArray(task.tests)) return task.tests;
    if (task.tests && Array.isArray(task.tests.items)) return task.tests.items;
    if (task.tests && typeof task.tests === "object") return [task.tests];
    return [];
  }

  function renderTaskTests(task) {
    const panel = element("engineering-task-pane-tests");
    panel.replaceChildren();
    detailHeading(panel, "測試與驗證");
    const tests = normalizeTests(task);
    const validations = Array.isArray(task.worker_validations) ? task.worker_validations : [];
    if (!tests.length && !validations.length) {
      emptyDetailState(panel, "尚無驗證結果；「未執行」不會被標示為測試失敗。");
      return;
    }
    if (tests.length) {
      const list = makeElement("div", "task-test-list");
      tests.forEach((test) => {
        const card = makeElement("article", "task-test");
        const status = test.status || (test.exit_code === 0 ? "done" : (test.exit_code == null ? "unknown" : "failed"));
        const badge = document.createElement("span");
        setStatusBadge(badge, status);
        card.append(badge, makeElement("strong", "", test.label || test.name || test.kind || "Validation"));
        if (test.summary || test.message) card.append(makeElement("p", "", test.summary || test.message));
        if (test.exit_code != null) card.append(makeElement("span", "task-metadata", `exit ${test.exit_code}`));
        list.append(card);
      });
      panel.append(list);
    } else {
      emptyDetailState(panel, "尚無 Server A 測試摘要。");
    }

    const validationSection = makeElement("section", "task-detail-section");
    validationSection.append(makeElement("h4", "", "Worker validations"));
    if (!validations.length) {
      emptyDetailState(validationSection, "尚無後續 worker validation request。");
      panel.append(validationSection);
      return;
    }
    const validationList = makeElement("div", "task-test-list");
    validations.forEach((validation) => {
      const card = makeElement("article", "task-test");
      const badge = document.createElement("span");
      setStatusBadge(badge, validation.status || "unknown");
      card.append(
        badge,
        makeElement("strong", "", validation.id ? `Validation ${validation.id}` : "Worker validation")
      );
      const connection = validation.connection && typeof validation.connection === "object"
        ? validation.connection.code
        : null;
      const metadata = [
        validation.attempt_number != null && `Attempt ${validation.attempt_number}`,
        validation.approval_id != null && `Approval #${validation.approval_id}`,
        connection && `Connection ${connection}`,
        validation.created_at && `created ${validation.created_at}`,
        validation.updated_at && `updated ${validation.updated_at}`,
      ].filter(Boolean).join(" · ");
      if (metadata) card.append(makeElement("span", "task-metadata", metadata));
      const result = validation.result && typeof validation.result === "object"
        ? validation.result
        : null;
      if (result) {
        const resultMetadata = [
          result.status && `Result ${result.status}`,
          result.exit_code != null && `exit ${result.exit_code}`,
          result.finished_at && `finished ${result.finished_at}`,
        ].filter(Boolean).join(" · ");
        if (resultMetadata) card.append(makeElement("span", "task-metadata", resultMetadata));
      }
      validationList.append(card);
    });
    validationSection.append(validationList);
    panel.append(validationSection);
  }

  function renderTaskArtifacts(task) {
    const panel = element("engineering-task-pane-artifacts");
    panel.replaceChildren();
    detailHeading(panel, "Artifacts");
    const artifacts = Array.isArray(task.artifacts) ? task.artifacts : [];
    if (!artifacts.length) {
      emptyDetailState(panel, "尚無 artifact metadata。清理後的記錄應保留歷史，並由後端標示 availability。");
      return;
    }
    const list = makeElement("ul", "task-artifact-list");
    artifacts.forEach((artifact) => {
      const item = makeElement("li", "task-artifact");
      item.append(makeElement("strong", "", artifact.label || artifact.kind || artifact.artifact_key || "Artifact"));
      item.append(makeElement("p", "", "Artifact 儲存位置不公開"));
      const metadata = [
        artifact.availability,
        artifact.verification_status,
        artifact.redaction_status,
        artifact.sha256 && `sha256 ${artifact.sha256}`,
        artifact.size_bytes != null && `${artifact.size_bytes} bytes`,
      ].filter(Boolean).join(" · ");
      if (metadata) item.append(makeElement("span", "task-artifact-meta", metadata));
      list.append(item);
    });
    panel.append(list);
  }

  function renderTaskRisks(task) {
    const panel = element("engineering-task-pane-risks");
    panel.replaceChildren();
    detailHeading(panel, "Risks / Warnings");
    const presentation = taskPresentation(task);
    const candidates = [
      ...(Array.isArray(task.warnings) ? task.warnings : []),
      ...(Array.isArray(presentation.warnings) ? presentation.warnings : []),
    ];
    const warnings = Array.from(new Set(
      candidates
        .filter((warning) => typeof warning === "string")
        .map((warning) => warning.trim())
        .filter(Boolean)
    ));
    const safetyFlags = [];
    if (task.final_response && task.final_response.withheld === true) {
      safetyFlags.push("最終回覆已由伺服器安全政策隱藏。");
    }
    if (task.changes && task.changes.withheld === true) {
      safetyFlags.push("Diff 已由伺服器安全政策隱藏。");
    }
    if (!warnings.length && !safetyFlags.length) {
      emptyDetailState(panel, "伺服器目前未回傳風險或警告；這不代表已完成全面安全審查。");
      return;
    }
    const list = makeElement("ul", "task-risk-list");
    [...warnings, ...safetyFlags].forEach((warning) => {
      const item = makeElement("li", "ui-alert ui-alert-warning");
      item.append(
        makeElement("span", "", "!"),
        makeElement("div", "", warning)
      );
      list.append(item);
    });
    panel.append(list);
  }

  function renderTaskApprovals(task) {
    const panel = element("engineering-task-pane-approvals");
    panel.replaceChildren();
    detailHeading(panel, "核准歷史");
    const approvals = Array.isArray(task.approval_history) ? task.approval_history : [];
    if (!approvals.length) {
      emptyDetailState(panel, "尚無可顯示的核准歷史。");
      return;
    }
    const list = makeElement("ol", "task-approval-list");
    approvals.forEach((approval) => {
      const item = makeElement("li", "task-approval");
      item.append(makeElement("strong", "", approval.label || approval.kind || "Approval"));
      item.append(makeElement("p", "", approval.status || "unknown"));
      const actor = approval.actor_display || approval.approver_display || approval.requester_display
        || identityDisplay(approval.actor || approval.approver || approval.requester, "");
      const approvalId = approval.id || approval.approval_id;
      const meta = [approvalId && `#${approvalId}`, actor, approval.created_at || approval.decided_at]
        .filter(Boolean).join(" · ");
      if (meta) item.append(makeElement("span", "task-approval-meta", meta));
      if (approval.reason) item.append(makeElement("p", "", approval.reason));
      list.append(item);
    });
    panel.append(list);
  }

  function availableAction(task, names) {
    const actions = task && task.available_actions;
    if (!actions) return null;
    if (Array.isArray(actions)) {
      for (const action of actions) {
        const key = typeof action === "string" ? action : action && (action.action || action.name || action.id);
        if (names.includes(key)) {
          return typeof action === "string" ? { enabled: true, name: key } : action;
        }
      }
      return null;
    }
    if (typeof actions === "object") {
      for (const name of names) {
        if (Object.prototype.hasOwnProperty.call(actions, name)) {
          const value = actions[name];
          return typeof value === "boolean" ? { enabled: value, name } : { name, ...(value || {}) };
        }
      }
    }
    return null;
  }

  function linkedCodingRunId(task, action) {
    if (action && action.coding_run_id != null) return action.coding_run_id;
    if (task && task.coding_run && task.coding_run.id != null) return task.coding_run.id;
    return task && task.coding_run_id != null ? task.coding_run_id : null;
  }

  function canonicalEngineeringTaskUuid(value) {
    const taskId = String(value || "");
    return /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(taskId)
      ? taskId
      : null;
  }

  function authorizedPatchDownload(task) {
    const action = availableAction(task, ["download_patch"]);
    const taskId = canonicalEngineeringTaskUuid(task && task.id);
    const expectedUrl = taskId
      ? `/engineering-tasks/${encodeURIComponent(taskId)}/patch`
      : null;
    const url = action && typeof action.url === "string" ? action.url : null;
    return {
      action,
      taskId,
      url: action && action.enabled === true && url === expectedUrl ? url : null,
    };
  }

  function captureTaskDetailAction() {
    return Object.freeze({
      taskId: state.detailTaskId,
      detailSerial: state.detailOpenSerial,
      detailTask: state.detailTask,
    });
  }

  function taskDetailActionIsCurrent(snapshot) {
    const dialog = element("engineering-task-detail-dialog");
    return Boolean(
      snapshot
      && snapshot.taskId
      && snapshot.detailTask
      && snapshot.detailSerial === state.detailOpenSerial
      && snapshot.taskId === state.detailTaskId
      && snapshot.detailTask === state.detailTask
      && dialog
      && !dialog.hidden
    );
  }

  function renderTaskActions(task) {
    const validation = availableAction(task, [
      "request_worker_validation", "request_validation", "worker_validation", "validation",
    ]);
    const cleanup = availableAction(task, ["cleanup", "cleanup_worktree"]);
    const validationRunId = linkedCodingRunId(task, validation);
    const cleanupRunId = linkedCodingRunId(task, cleanup);
    const retry = availableAction(task, ["retry"]);
    const discard = availableAction(task, ["discard"]);
    const promote = availableAction(task, ["promote"]);
    const validationButton = element("engineering-task-validation-action");
    const cleanupButton = element("engineering-task-cleanup-action");
    const patchButton = element("engineering-task-download-patch-action");
    const retryButton = element("engineering-task-retry-action");
    const discardButton = element("engineering-task-discard-action");
    const promoteButton = element("engineering-task-promote-action");
    const patchDownload = authorizedPatchDownload(task);
    validationButton.removeAttribute("aria-busy");
    cleanupButton.removeAttribute("aria-busy");
    retryButton.removeAttribute("aria-busy");
    discardButton.removeAttribute("aria-busy");
    promoteButton.removeAttribute("aria-busy");
    validationButton.disabled = !(validation && validation.enabled === true && validationRunId != null);
    cleanupButton.disabled = !(cleanup && cleanup.enabled === true && cleanupRunId != null);
    patchButton.disabled = !patchDownload.url;
    retryButton.disabled = !(retry && retry.enabled === true);
    discardButton.disabled = !(discard && discard.enabled === true);
    promoteButton.disabled = !(promote && promote.enabled === true);
    retryButton.dataset.engineeringTaskId = String(task.id || "");
    discardButton.dataset.engineeringTaskId = String(task.id || "");
    promoteButton.dataset.engineeringTaskId = String(task.id || "");
    retryButton.title = retry && retry.reason ? String(retry.reason) : "伺服器未開啟此動作";
    discardButton.title = discard && discard.reason ? String(discard.reason) : "伺服器未開啟此動作";
    promoteButton.title = promote && promote.reason ? String(promote.reason) : "建立人工 promotion 核准請求";
    validationButton.dataset.codingRunId = validationRunId == null ? "" : String(validationRunId);
    validationButton.dataset.engineeringTaskId = validation && validation.engineering_task_id
      ? String(validation.engineering_task_id)
      : String(task.id || "");
    validationButton.dataset.requestMode = validation && validation.request_mode
      ? String(validation.request_mode)
      : (task.legacy ? "legacy_dispatch" : "native_pending_approval");
    cleanupButton.dataset.codingRunId = cleanupRunId == null ? "" : String(cleanupRunId);
    patchButton.dataset.downloadUrl = patchDownload.url || "";
    patchButton.dataset.taskId = patchDownload.taskId || "";
    validationButton.title = validation && validation.reason ? String(validation.reason) : "伺服器未開啟此動作";
    cleanupButton.title = cleanup && cleanup.reason ? String(cleanup.reason) : "伺服器未開啟此動作";
    patchButton.title = patchDownload.url
      ? "下載伺服器授權的去敏後的已收集 patch"
      : (patchDownload.action && patchDownload.action.reason
          ? String(patchDownload.action.reason)
          : "伺服器未回傳可用的去敏後的已收集 patch");

    const futureKeys = {
      continue: ["continue"],
      request_changes: ["request_changes"],
      cancel: ["cancel"],
      finalize: ["finalize"],
      download_bundle: ["download_bundle"],
      create_draft_pr: ["create_draft_pr"],
    };
    document.querySelectorAll("[data-future-task-action]").forEach((button) => {
      if (button.dataset.futureTaskAction === "download_patch") return;
      const action = availableAction(task, futureKeys[button.dataset.futureTaskAction] || []);
      // Raw bundles and all remaining future actions stay disabled even if a
      // server accidentally advertises them.  Only the separately handled,
      // sanitized collected-patch action has a client download path.
      button.disabled = true;
      button.title = button.dataset.futureTaskAction === "download_bundle"
        ? "原始 bundle 可能含未去敏內容，平台不提供下載"
        : (action && action.reason ? String(action.reason) : "尚無後端動作端點");
    });
    const reasons = [];
    if (validation && validation.reason) reasons.push(`後續驗證：${validation.reason}`);
    if (cleanup && cleanup.reason) reasons.push(`清理：${cleanup.reason}`);
    if (retry && retry.reason) reasons.push(`Retry：${retry.reason}`);
    if (discard && discard.reason) reasons.push(`Discard：${discard.reason}`);
    if (promote && promote.reason) reasons.push(`Promote：${promote.reason}`);
    const enabledCount = Number(!validationButton.disabled)
      + Number(!cleanupButton.disabled)
      + Number(!patchButton.disabled)
      + Number(!retryButton.disabled)
      + Number(!discardButton.disabled)
      + Number(!promoteButton.disabled);
    element("engineering-task-action-note").textContent = reasons.length
      ? reasons.join("；")
      : (enabledCount
          ? "僅開啟伺服器 available_actions 明確允許的現有動作。"
          : "伺服器未回傳可執行的現有動作。");
  }

  function renderEngineeringTaskDetail(task) {
    const options = arguments.length > 1 && arguments[1] ? arguments[1] : {};
    const activePane = options.activePane || "overview";
    state.detailTask = task;
    const status = taskStatus(task);
    const structured = task.structured_request || {};
    const title = task.title || task.objective || structured.objective
      || `AI 工程任務 ${task.id || ""}`;
    element("engineering-task-detail-title").textContent = title;
    setStatusBadge(
      element("engineering-task-detail-status"),
      status,
      presentationLabel(taskPresentation(task).state)
    );
    renderTaskOverview(task);
    renderTaskTimeline(task);
    renderTaskCommands(task);
    renderTaskChanges(task);
    renderTaskTests(task);
    renderTaskArtifacts(task);
    renderTaskRisks(task);
    renderTaskApprovals(task);
    renderTaskActions(task);
    element("engineering-task-detail-state").hidden = true;
    element("engineering-task-detail-content").hidden = false;
    element("engineering-task-detail-actions").hidden = false;
    activateTaskPane(activePane, { focus: false });
  }

  async function loadTaskDiff({ focusState = false } = {}) {
    if (state.detailDiffLoaded || state.detailDiffLoading || !state.detailTaskId) return;
    const task = state.detailTask;
    if (!task) return;
    const taskId = state.detailTaskId;
    const changes = task.changes && typeof task.changes === "object" ? task.changes : {};
    const target = element("engineering-task-diff-state");
    if (!target || changes.withheld === true || changes.available !== true) return;
    state.detailDiffLoading = true;
    setTaskDiffState("loading", "正在載入經遮罩的 Diff", "只讀取後端允許且已遮罩的差異內容。");
    if (focusState) {
      target.tabIndex = -1;
      target.focus({ preventScroll: true });
    }
    const serial = state.detailOpenSerial;
    const safeDefault = `/engineering-tasks/${encodeURIComponent(taskId)}/diff`;
    const endpoint = typeof changes.diff_url === "string" && changes.diff_url === safeDefault
      ? changes.diff_url
      : safeDefault;
    try {
      const response = await api(endpoint);
      if (
        serial !== state.detailOpenSerial
        || state.detailTaskId !== taskId
        || state.detailTask !== task
        || !element("engineering-task-diff-state")
      ) return;
      if (response && response.withheld === true) {
        target.className = "ui-alert ui-alert-warning";
        target.setAttribute("role", "note");
        target.replaceChildren(makeElement(
          "div",
          "",
          "Diff 因安全政策而整段隱藏；瀏覽器不會顯示回應中的其他欄位。"
        ));
        state.detailDiffLoaded = true;
        return;
      }
      if (!response || response.available !== true || typeof response.patch !== "string") {
        setTaskDiffState(
          "error",
          "Diff 安全投影不完整",
          "後端未明確回傳 available=true 與字串 patch；內容維持隱藏。",
          { focusRetry: focusState }
        );
        return;
      }
      const patch = response.patch;
      const viewer = makeElement("pre", "task-diff-viewer", patch || "（無 diff）");
      viewer.tabIndex = 0;
      target.className = "task-diff-container";
      target.removeAttribute("role");
      target.removeAttribute("aria-live");
      target.removeAttribute("aria-atomic");
      target.replaceChildren(viewer);
      state.detailDiffLoaded = true;
      if (focusState) {
        window.requestAnimationFrame(() => viewer.focus({ preventScroll: true }));
      }
    } catch (error) {
      if (
        serial !== state.detailOpenSerial
        || state.detailTaskId !== taskId
        || state.detailTask !== task
        || !element("engineering-task-diff-state")
      ) return;
      const disconnected = isConnectionError(error);
      setTaskDiffState(
        disconnected ? "disconnected" : "error",
        disconnected ? "Diff 連線中斷" : "Diff 載入失敗",
        disconnected
          ? "disconnected 不代表任務 failed；請恢復連線後重試。"
          : String(error.message || error),
        { focusRetry: focusState }
      );
    } finally {
      if (
        serial === state.detailOpenSerial
        && state.detailTaskId === taskId
        && state.detailTask === task
      ) state.detailDiffLoading = false;
    }
  }

  function activateTaskPane(name, { focus = true } = {}) {
    const tabs = Array.from(document.querySelectorAll("[data-task-pane]"));
    const selected = tabs.find((tab) => tab.dataset.taskPane === name) || tabs[0];
    tabs.forEach((tab) => {
      const active = tab === selected;
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
    document.querySelectorAll("[data-task-panel]").forEach((panel) => {
      panel.hidden = panel.dataset.taskPanel !== selected.dataset.taskPane;
    });
    if (focus) selected.focus();
    if (selected.dataset.taskPane === "changes") loadTaskDiff();
  }

  function handleTaskTabsKeydown(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = Array.from(document.querySelectorAll("[data-task-pane]"));
    const current = tabs.indexOf(event.target);
    if (current < 0) return;
    event.preventDefault();
    let next = current;
    if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else next = (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    activateTaskPane(tabs[next].dataset.taskPane);
  }

  async function openEngineeringTaskDetail(taskId, { preserveOpener = false } = {}) {
    if (!engineeringTaskDetailEnabled() || !taskId) return;
    if (!element("engineering-task-dialog").hidden) {
      closeEngineeringTaskWizard({ restoreFocus: false });
    }
    const dialog = element("engineering-task-detail-dialog");
    if (!preserveOpener) state.detailOpener = document.activeElement;
    state.detailTaskId = String(taskId);
    state.detailTask = null;
    state.detailDiffLoaded = false;
    state.detailDiffLoading = false;
    resetTaskEventPaging([]);
    const serial = ++state.detailOpenSerial;
    element("engineering-task-detail-refresh-btn").disabled = false;
    element("engineering-task-detail-refresh-btn").removeAttribute("aria-busy");
    activateTaskPane("overview", { focus: false });
    element("engineering-task-detail-title").textContent = `AI 工程任務 ${state.detailTaskId}`;
    setStatusBadge(element("engineering-task-detail-status"), "unknown");
    setTaskDetailState("loading", "正在載入任務詳情", "取得進度、命令、變更、驗證與核准紀錄。");
    isolateTaskDetailBackground();
    dialog.hidden = false;
    element("modal-backdrop").classList.add("open");
    document.body.classList.add("dialog-open");
    window.requestAnimationFrame(() => element("engineering-task-detail-title").focus());
    try {
      const detail = await api(`/engineering-tasks/${encodeURIComponent(state.detailTaskId)}`);
      if (serial !== state.detailOpenSerial || dialog.hidden) return;
      renderEngineeringTaskDetail(detail, { activePane: "overview" });
    } catch (error) {
      if (serial !== state.detailOpenSerial || dialog.hidden) return;
      const disconnected = isConnectionError(error);
      setTaskDetailState(
        disconnected ? "disconnected" : "error",
        disconnected ? "任務詳情連線中斷" : "任務詳情載入失敗",
        disconnected ? "disconnected 不代表任務 failed；請恢復連線後重試。" : String(error.message || error)
      );
    }
  }

  function retryEngineeringTaskDetail() {
    const dialog = element("engineering-task-detail-dialog");
    const taskId = state.detailTaskId;
    if (!taskId || !dialog || dialog.hidden) return;
    element("engineering-task-detail-title").focus({ preventScroll: true });
    refreshEngineeringTaskDetail();
  }

  async function refreshEngineeringTaskDetail() {
    const dialog = element("engineering-task-detail-dialog");
    const taskId = state.detailTaskId;
    if (!taskId || !dialog || dialog.hidden) return;
    const selected = dialog.querySelector('[data-task-pane][aria-selected="true"]');
    const activePane = selected ? selected.dataset.taskPane : "overview";
    const refreshButton = element("engineering-task-detail-refresh-btn");
    const serial = ++state.detailOpenSerial;
    state.detailTask = null;
    state.detailDiffLoaded = false;
    state.detailDiffLoading = false;
    resetTaskEventPaging([]);
    refreshButton.disabled = true;
    refreshButton.setAttribute("aria-busy", "true");
    setStatusBadge(element("engineering-task-detail-status"), "unknown", "重新整理中");
    setTaskDetailState(
      "loading",
      "正在重新整理任務詳情",
      "重新取得進度、驗證、風險與伺服器允許的可用動作。"
    );
    try {
      const detail = await api(`/engineering-tasks/${encodeURIComponent(taskId)}`);
      if (serial !== state.detailOpenSerial || dialog.hidden || state.detailTaskId !== taskId) return;
      renderEngineeringTaskDetail(detail, { activePane });
    } catch (error) {
      if (serial !== state.detailOpenSerial || dialog.hidden || state.detailTaskId !== taskId) return;
      const disconnected = isConnectionError(error);
      setStatusBadge(
        element("engineering-task-detail-status"),
        disconnected ? "disconnected" : "unknown"
      );
      setTaskDetailState(
        disconnected ? "disconnected" : "error",
        disconnected ? "任務詳情連線中斷" : "任務詳情重新整理失敗",
        disconnected
          ? "disconnected 不代表任務 failed；舊的動作能力已停用，恢復連線後再重試。"
          : "無法取得最新安全投影；舊的動作能力已停用，請重試。"
      );
    } finally {
      if (serial === state.detailOpenSerial && !dialog.hidden) {
        refreshButton.disabled = false;
        refreshButton.removeAttribute("aria-busy");
      }
    }
  }

  function closeEngineeringTaskDetail({ restoreFocus = true } = {}) {
    const dialog = element("engineering-task-detail-dialog");
    if (!dialog || dialog.hidden) return;
    const closingTaskId = state.detailTaskId;
    ++state.detailOpenSerial;
    dialog.hidden = true;
    restoreTaskDetailBackground();
    state.detailTaskId = null;
    state.detailTask = null;
    state.detailDiffLoaded = false;
    state.detailDiffLoading = false;
    resetTaskEventPaging([]);
    element("engineering-task-detail-refresh-btn").disabled = false;
    element("engineering-task-detail-refresh-btn").removeAttribute("aria-busy");
    if (element("engineering-task-dialog").hidden && !document.querySelector(".modal.open")) {
      element("modal-backdrop").classList.remove("open");
      document.body.classList.remove("dialog-open");
    }
    const opener = state.detailOpener;
    state.detailOpener = null;
    if (restoreFocus) {
      const replacement = Array.from(document.querySelectorAll("[data-engineering-task-open]"))
        .find((button) => button.dataset.taskId === closingTaskId);
      const focusTarget = opener && opener.isConnected ? opener : (replacement || element("coding-runs-refresh-btn"));
      if (focusTarget && typeof focusTarget.focus === "function") {
        window.requestAnimationFrame(() => focusTarget.focus());
      }
    }
  }

  function handleTaskDetailKeydown(event) {
    const dialog = element("engineering-task-detail-dialog");
    if (dialog.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeEngineeringTaskDetail();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = focusableElements(dialog);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function runTaskValidationAction() {
    const button = element("engineering-task-validation-action");
    const snapshot = captureTaskDetailAction();
    const engineeringTaskId = button.dataset.engineeringTaskId;
    const codingRunId = button.dataset.codingRunId;
    const requestMode = button.dataset.requestMode;
    if (
      button.disabled
      || !codingRunId
      || !taskDetailActionIsCurrent(snapshot)
    ) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await window.openValidationDispatchFromEngineeringTask(
        engineeringTaskId,
        codingRunId,
        requestMode,
        () => taskDetailActionIsCurrent(snapshot),
      );
      if (!taskDetailActionIsCurrent(snapshot)) return;
      closeEngineeringTaskDetail({ restoreFocus: false });
    } catch (error) {
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast(`無法開啟後續驗證：${String(error.message || error)}`);
      renderTaskActions(snapshot.detailTask);
    }
  }

  async function runTaskCleanupAction() {
    const button = element("engineering-task-cleanup-action");
    const snapshot = captureTaskDetailAction();
    const runId = button.dataset.codingRunId;
    if (
      button.disabled
      || !runId
      || !taskDetailActionIsCurrent(snapshot)
    ) return;
    if (!window.confirm(`確定要清理 coding_run #${runId} 的 worktree 嗎？此動作無法復原。`)) return;
    if (!taskDetailActionIsCurrent(snapshot)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await api(`/coding-runs/${encodeURIComponent(runId)}/cleanup`, { method: "POST" });
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast("已清理 worktree");
      await loadEngineeringTasks();
      if (!taskDetailActionIsCurrent(snapshot)) return;
      button.removeAttribute("aria-busy");
      const refreshPromise = refreshEngineeringTaskDetail();
      const refreshSerial = state.detailOpenSerial;
      await refreshPromise;
      if (
        state.detailTaskId !== snapshot.taskId
        || state.detailOpenSerial !== refreshSerial
      ) return;
    } catch (error) {
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast(`清理失敗：${String(error.message || error)}`);
      renderTaskActions(snapshot.detailTask);
    }
  }

  async function runTaskRetryAction() {
    const button = element("engineering-task-retry-action");
    const snapshot = captureTaskDetailAction();
    const taskId = button.dataset.engineeringTaskId;
    if (button.disabled || !taskId || !taskDetailActionIsCurrent(snapshot)) return;
    if (!window.confirm("確定要對這個 AI 工程任務建立新的 attempt（retry）核准請求嗎？")) return;
    if (!taskDetailActionIsCurrent(snapshot)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await api(`/engineering-tasks/${encodeURIComponent(taskId)}/retry-request`, {
        method: "POST",
      });
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast("已建立 retry 核准請求（需人工核准）");
      await loadEngineeringTasks();
      if (!taskDetailActionIsCurrent(snapshot)) return;
      button.removeAttribute("aria-busy");
      const refreshPromise = refreshEngineeringTaskDetail();
      const refreshSerial = state.detailOpenSerial;
      await refreshPromise;
      if (
        state.detailTaskId !== snapshot.taskId
        || state.detailOpenSerial !== refreshSerial
      ) return;
    } catch (error) {
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast(`建立 retry 請求失敗：${String(error.message || error)}`);
      renderTaskActions(snapshot.detailTask);
    }
  }

  async function runTaskDiscardAction() {
    const button = element("engineering-task-discard-action");
    const snapshot = captureTaskDetailAction();
    const taskId = button.dataset.engineeringTaskId;
    if (button.disabled || !taskId || !taskDetailActionIsCurrent(snapshot)) return;
    if (!window.confirm(
      "確定要對這個 AI 工程任務建立作廢（discard）核准請求嗎？"
      + "核准後結果與 artifact 會視同 withheld，此動作不會刪除 Runner 上的工作區。"
    )) return;
    if (!taskDetailActionIsCurrent(snapshot)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await api(`/engineering-tasks/${encodeURIComponent(taskId)}/discard-request`, {
        method: "POST",
      });
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast("已建立 discard 核准請求（需人工核准）");
      await loadEngineeringTasks();
      if (!taskDetailActionIsCurrent(snapshot)) return;
      button.removeAttribute("aria-busy");
      const refreshPromise = refreshEngineeringTaskDetail();
      const refreshSerial = state.detailOpenSerial;
      await refreshPromise;
      if (
        state.detailTaskId !== snapshot.taskId
        || state.detailOpenSerial !== refreshSerial
      ) return;
    } catch (error) {
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast(`建立 discard 請求失敗：${String(error.message || error)}`);
      renderTaskActions(snapshot.detailTask);
    }
  }

  async function runTaskPromoteAction() {
    const button = element("engineering-task-promote-action");
    const snapshot = captureTaskDetailAction();
    const taskId = button.dataset.engineeringTaskId;
    if (button.disabled || !taskId || !taskDetailActionIsCurrent(snapshot)) return;
    if (!window.confirm(
      "建立 Promote to Hub 核准請求？這一步只固定 bundle/commit，"
      + "仍需具核准權限的人在核准卡確認後才會 publish。"
    )) return;
    if (!taskDetailActionIsCurrent(snapshot)) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      await api(`/engineering-tasks/${encodeURIComponent(taskId)}/promote-request`, {
        method: "POST",
      });
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast("已建立 Promote to Hub 核准請求（尚未 publish）");
      await loadEngineeringTasks();
      if (!taskDetailActionIsCurrent(snapshot)) return;
      button.removeAttribute("aria-busy");
      const refreshPromise = refreshEngineeringTaskDetail();
      const refreshSerial = state.detailOpenSerial;
      await refreshPromise;
      if (
        state.detailTaskId !== snapshot.taskId
        || state.detailOpenSerial !== refreshSerial
      ) return;
    } catch (error) {
      if (!taskDetailActionIsCurrent(snapshot)) return;
      showToast(`建立 promotion 請求失敗：${String(error.message || error)}`);
      renderTaskActions(snapshot.detailTask);
    }
  }

  async function runTaskPatchDownloadAction() {
    const button = element("engineering-task-download-patch-action");
    const detailSerial = state.detailOpenSerial;
    const patchDownload = authorizedPatchDownload(state.detailTask || {});
    if (
      button.disabled ||
      !patchDownload.url ||
      button.dataset.downloadUrl !== patchDownload.url ||
      button.dataset.taskId !== patchDownload.taskId
    ) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    try {
      const result = await authenticatedDownload(patchDownload.url);
      if (
        detailSerial !== state.detailOpenSerial ||
        canonicalEngineeringTaskUuid(state.detailTask && state.detailTask.id) !== patchDownload.taskId
      ) {
        throw new Error("任務詳情已變更");
      }
      const filename = `engineering-task-${patchDownload.taskId}${result.redacted ? ".redacted" : ""}.patch`;
      const objectUrl = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      anchor.hidden = true;
      document.body.append(anchor);
      try {
        anchor.click();
      } finally {
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
      }
      showToast("已下載去敏後的已收集 patch");
    } catch (error) {
      if (detailSerial === state.detailOpenSerial) {
        showToast(`去敏後的已收集 patch 下載失敗：${String(error.message || error)}`);
      }
    } finally {
      button.removeAttribute("aria-busy");
      if (detailSerial === state.detailOpenSerial && state.detailTask) {
        renderTaskActions(state.detailTask);
      } else {
        button.disabled = true;
      }
    }
  }

  function handleEngineeringTaskListClick(event) {
    const trigger = event.target.closest("[data-engineering-task-open]");
    if (!trigger || !element("coding-runs-tbody").contains(trigger)) return;
    openEngineeringTaskDetail(trigger.dataset.taskId);
  }

  function navigateProjectWorkspace(section) {
    if (!state.currentProject || !state.currentProject.name) return;
    const nextHash = projectWorkspaceHash(state.currentProject.name, section);
    if (window.location.hash === nextHash) {
      activateProjectWorkspaceSection(section, { focus: true });
      updatePageContext();
    } else {
      window.location.hash = nextHash;
    }
  }

  function handleProjectSubnavigationClick(event) {
    const control = event.target.closest("[data-project-section]");
    if (!control || !element("project-subnavigation").contains(control)) return;
    navigateProjectWorkspace(control.dataset.projectSection);
  }

  function handleProjectSubnavigationKeydown(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const controls = Array.from(
      element("project-subnavigation").querySelectorAll("[data-project-section]")
    );
    const currentIndex = controls.indexOf(event.target);
    if (currentIndex < 0) return;
    event.preventDefault();
    let nextIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = controls.length - 1;
    else if (event.key === "ArrowLeft") {
      nextIndex = (currentIndex - 1 + controls.length) % controls.length;
    } else {
      nextIndex = (currentIndex + 1) % controls.length;
    }
    controls[nextIndex].focus();
    navigateProjectWorkspace(controls[nextIndex].dataset.projectSection);
  }

  function initialize() {
    if (state.initialized) return;
    state.initialized = true;
    state.legacyOpenCodingTaskModal = window.openCodingTaskModal;
    if (flags.AI_ENGINEERING_TASK_UI_V2) {
      window.openCodingTaskModal = openEngineeringTaskWizard;
    }

    element("mobile-nav-open-btn").addEventListener("click", openNavigation);
    element("mobile-nav-close-btn").addEventListener("click", () =>
      closeNavigation({ restoreFocus: true })
    );
    element("navigation-scrim").addEventListener("click", () =>
      closeNavigation({ restoreFocus: true })
    );
    document.querySelectorAll("nav.tabs button").forEach((button) => {
      button.addEventListener("click", () => {
        if (!mobileNavigationActive()) return;
        window.setTimeout(() => {
          closeNavigation();
          element("page-title").tabIndex = -1;
          element("page-title").focus({ preventScroll: true });
        }, 0);
      });
    });
    element("project-subnavigation").addEventListener(
      "click", handleProjectSubnavigationClick
    );
    element("project-subnavigation").addEventListener(
      "keydown", handleProjectSubnavigationKeydown
    );
    window.addEventListener("hashchange", () => {
      updatePageContext();
      closeNavigation();
    });
    window.addEventListener("resize", () => {
      if (window.innerWidth > 768) closeNavigation();
      else syncNavigationAccessibility();
    });
    document.addEventListener("keydown", (event) => {
      trapNavigationFocus(event);
      if (event.key === "Escape" && document.body.classList.contains("navigation-open")) {
        closeNavigation({ restoreFocus: true });
      }
    });

    element("engineering-task-close-btn").addEventListener("click", () =>
      closeEngineeringTaskWizard()
    );
    element("engineering-cancel-btn").addEventListener("click", () =>
      closeEngineeringTaskWizard()
    );
    element("engineering-previous-btn").addEventListener("click", () =>
      showStep(state.step - 1)
    );
    element("engineering-next-btn").addEventListener("click", () => {
      if (validateStep(state.step)) showStep(state.step + 1);
    });
    element("engineering-task-form").addEventListener("submit", submitEngineeringTask);
    element("engineering-task-dialog").addEventListener("keydown", handleDialogKeydown);
    element("modal-backdrop").addEventListener("click", () => {
      if (!element("engineering-task-dialog").hidden) closeEngineeringTaskWizard();
    });

    element("engineering-worker-validation").addEventListener("change", (event) => {
      element("engineering-validation-target").disabled = !event.target.checked;
      if (!event.target.checked) element("engineering-validation-target").value = "";
      updatePreview();
    });
    element("engineering-task-form").addEventListener("input", () => {
      if (element("engineering-objective").value.trim()) {
        element("engineering-objective").removeAttribute("aria-invalid");
        element("engineering-objective-error").hidden = true;
      }
      if (element("engineering-allowed-scope").value.trim()) {
        element("engineering-allowed-scope").removeAttribute("aria-invalid");
        element("engineering-allowed-scope-error").hidden = true;
      }
      element("engineering-prohibited-paths").removeAttribute("aria-invalid");
      element("engineering-prohibited-paths-error").hidden = true;
      clearPathPolicyCoverageResult();
      updatePreview();
    });
    element("engineering-task-form").addEventListener("change", () => {
      if (element("engineering-validation-target").value) {
        element("engineering-validation-target").removeAttribute("aria-invalid");
        element("engineering-validation-error").hidden = true;
      }
      updatePreview();
    });
    element("engineering-version-reference").addEventListener("change", () => {
      clearPathPolicyCoverageResult();
      updatePreview();
    });
    element("engineering-path-coverage-btn").addEventListener(
      "click", checkPathPolicyCoverage
    );
    element("coding-runs-tbody").addEventListener("click", handleEngineeringTaskListClick);
    element("engineering-task-detail-refresh-btn").addEventListener(
      "click", refreshEngineeringTaskDetail
    );
    element("engineering-task-detail-close-btn").addEventListener("click", () =>
      closeEngineeringTaskDetail()
    );
    element("engineering-task-detail-dialog").addEventListener("keydown", handleTaskDetailKeydown);
    element("engineering-task-detail-dialog").querySelector("[role=tablist]").addEventListener("click", (event) => {
      const tab = event.target.closest("[data-task-pane]");
      if (tab) activateTaskPane(tab.dataset.taskPane);
    });
    element("engineering-task-detail-dialog").querySelector("[role=tablist]").addEventListener("keydown", handleTaskTabsKeydown);
    element("engineering-task-validation-action").addEventListener("click", runTaskValidationAction);
    element("engineering-task-cleanup-action").addEventListener("click", runTaskCleanupAction);
    element("engineering-task-download-patch-action").addEventListener("click", runTaskPatchDownloadAction);
    element("engineering-task-retry-action").addEventListener("click", runTaskRetryAction);
    element("engineering-task-discard-action").addEventListener("click", runTaskDiscardAction);
    element("engineering-task-promote-action").addEventListener("click", runTaskPromoteAction);
    element("modal-backdrop").addEventListener("click", () => {
      if (!element("engineering-task-detail-dialog").hidden) closeEngineeringTaskDetail();
    });
    element("pd-run-request-refresh-btn").addEventListener("click", () => {
      if (runRequestState.projectName) loadRunRequestPanel(runRequestState.projectName);
    });
    element("pd-run-request-form").addEventListener("submit", submitRunRequest);
    syncNavigationAccessibility();
    updatePageContext();
  }

  // -------------------------------------------------------------------
  // 建立 Run（Execution Plan v1 request wizard, PERSONAL_PILOT_PLAN §6 T1）.
  //
  // Deliberately reuses the existing preview -> request contract rather than
  // inventing a new one: `POST .../execution-plans/preview` never writes
  // anything, so calling it before `POST .../runs/request` costs nothing and
  // lets a requester see every blocking reason before anything pending is
  // created. `dataset_none` is pinned true here; this form never offers a
  // dataset picker. `require_reproducible` is never sent, so it keeps the
  // backend default (true).
  // -------------------------------------------------------------------

  const PLAN_REASON_LABEL = Object.freeze({
    project_version_missing: "版本不存在或尚未指定",
    run_profile_missing: "缺少 Run Profile（本表單目前未提供選擇欄位）",
    run_profile_archived: "指定的 Run Profile 已封存",
    run_profile_requires_v2_compiler: "此 Run Profile 需要 v2 compiler",
    dataset_not_reproducible: "資料集不可重現（legacy registry dataset）",
    dataset_snapshot_not_published: "資料集 snapshot 尚未發布或未指定",
    target_not_approved: "目標機器尚未有 approved 的 server config revision",
    target_missing: "尚未選擇目標機器",
    command_missing: "尚未填寫 command",
    command_dangerous: "command 被判定為危險指令",
    plan_digest_mismatch: "plan digest 不一致",
  });

  const runRequestState = {
    loadSerial: 0,
    projectName: null,
    submitting: false,
    ready: false,
  };

  function runRequestSetOverallState(kind, title, message) {
    const box = element("pd-run-request-state");
    if (!box) return;
    box.textContent = "";
    const icon = { loading: "◌", empty: "∅", error: "×", disconnected: "↯", ready: "✓" }[kind] || "?";
    const wrap = document.createElement("div");
    wrap.className = `component-state state-${kind}`;
    wrap.setAttribute("role", "status");
    wrap.setAttribute("aria-live", "polite");
    const iconEl = document.createElement("div");
    iconEl.className = "component-state-icon";
    iconEl.setAttribute("aria-hidden", "true");
    iconEl.textContent = icon;
    const body = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const messageEl = document.createElement("p");
    messageEl.textContent = message;
    body.append(strong, messageEl);
    wrap.append(iconEl, body);
    box.append(wrap);
  }

  function runRequestSetControlsEnabled(enabled) {
    const allow = Boolean(enabled) && runRequestState.ready && !runRequestState.submitting;
    element("pd-run-request-version").disabled = !allow;
    element("pd-run-request-target").disabled = !allow;
    element("pd-run-request-command").disabled = !allow;
    element("pd-run-request-submit-btn").disabled = !allow;
  }

  function resetRunRequestPanel() {
    runRequestState.loadSerial += 1;
    runRequestState.projectName = null;
    runRequestState.submitting = false;
    runRequestState.ready = false;
    const form = element("pd-run-request-form");
    if (form) form.reset();
    element("pd-run-request-version").replaceChildren();
    element("pd-run-request-target").replaceChildren();
    element("pd-run-request-version-help").textContent = "";
    element("pd-run-request-target-help").textContent = "";
    element("pd-run-request-preview").hidden = true;
    element("pd-run-request-preview").textContent = "";
    element("pd-run-request-status").textContent = "";
    runRequestSetControlsEnabled(false);
    runRequestSetOverallState("loading", "正在等待專案", "開啟專案後才會載入版本與目標。");
  }

  async function loadRunRequestPanel(projectName) {
    const serial = ++runRequestState.loadSerial;
    runRequestState.projectName = projectName;
    runRequestState.submitting = false;
    runRequestState.ready = false;
    const form = element("pd-run-request-form");
    if (form) form.reset();
    element("pd-run-request-preview").hidden = true;
    element("pd-run-request-preview").textContent = "";
    element("pd-run-request-status").textContent = "";
    runRequestSetControlsEnabled(false);
    runRequestSetOverallState("loading", "正在載入版本與目標", "取得版本紀錄與已核准的 server config revision。");

    const versionSelect = element("pd-run-request-version");
    const targetSelect = element("pd-run-request-target");
    versionSelect.replaceChildren(new Option("載入中…", ""));
    targetSelect.replaceChildren(new Option("載入中…", ""));

    let versions = [];
    let versionsError = null;
    try {
      const raw = await api(`/projects/${encodeURIComponent(projectName)}/versions`);
      versions = Array.isArray(raw) ? raw : [];
    } catch (error) {
      versionsError = error;
    }
    if (serial !== runRequestState.loadSerial) return;

    // `GET /projects/{name}/versions` now carries `promotion_state`
    // (PERSONAL_PILOT_PLAN §6 T2 carry-over); only a promoted version may
    // back a reproducible run (P-3), so this list is filtered down to those
    // instead of asking the requester to self-verify promotion by hand.
    const promotedVersions = versions.filter(
      (version) => version && version.promotion_state === "promoted"
    );

    if (versionsError) {
      versionSelect.replaceChildren();
      versionSelect.disabled = true;
      element("pd-run-request-version-help").textContent =
        "版本清單載入失敗：" + String(versionsError.message || versionsError);
    } else if (!promotedVersions.length) {
      versionSelect.replaceChildren();
      versionSelect.disabled = true;
      element("pd-run-request-version-help").textContent =
        "尚無 promoted 版本，請先完成 engineering task promotion";
    } else {
      versionSelect.replaceChildren();
      promotedVersions.forEach((version) => {
        const commit = String(version.git_commit || "unknown").slice(0, 12);
        const ref = version.git_ref || "no ref";
        const created = version.created_at || "-";
        versionSelect.append(
          new Option(`${ref}@${commit}（${created}）`, version.id)
        );
      });
      element("pd-run-request-version-help").textContent =
        "此清單只列出已 promoted 的 ProjectVersion（新到舊）。";
    }

    const configs = typeof serverConfigsCache === "undefined" ? [] : serverConfigsCache;
    const targets = (configs || []).filter(
      (server) => server && server.server_config_revision_id
    );
    if (!targets.length) {
      targetSelect.replaceChildren();
      targetSelect.disabled = true;
      element("pd-run-request-target-help").textContent =
        "尚無擁有 approved/active server config revision 的機器，請先完成伺服器設定並核准。";
    } else {
      targetSelect.replaceChildren();
      targets.forEach((server) => {
        targetSelect.append(new Option(server.name, server.server_config_revision_id));
      });
      element("pd-run-request-target-help").textContent = "";
    }

    const ready = !versionsError && promotedVersions.length > 0 && targets.length > 0;
    runRequestState.ready = ready;
    runRequestSetControlsEnabled(true);
    if (ready) {
      runRequestSetOverallState(
        "ready",
        "可以建立 Run",
        "選擇版本、目標與 command 後送出；會先呼叫 preview 顯示 blocking reasons，通過後才會建立 pending approval。"
      );
    } else {
      runRequestSetOverallState(
        versionsError ? "error" : "empty",
        "尚無法建立 Run",
        "請先滿足上方版本或目標機器的前置條件。"
      );
    }
  }

  function runRequestReasonText(reasonCodes, missing) {
    const reasons = (Array.isArray(reasonCodes) ? reasonCodes : []).filter(
      (code) => code !== "plan_ready"
    );
    const lines = reasons.map((code) => `－ ${PLAN_REASON_LABEL[code] || code}`);
    if (Array.isArray(missing) && missing.length) {
      lines.push(`缺少欄位：${missing.join("、")}`);
    }
    return lines.join("\n");
  }

  function runRequestCollectInputs() {
    const versionSelect = element("pd-run-request-version");
    const targetSelect = element("pd-run-request-target");
    const commandField = element("pd-run-request-command");
    const command = commandField.value;
    if (!command || !command.trim()) {
      return { ok: false, error: "請填寫 command。" };
    }
    const projectVersionId = versionSelect.value || "";
    const serverConfigRevisionId = targetSelect.value || "";
    if (!projectVersionId) return { ok: false, error: "請選擇版本。" };
    if (!serverConfigRevisionId) return { ok: false, error: "請選擇目標機器。" };
    return {
      ok: true,
      body: {
        command,
        project_version_id: projectVersionId,
        server_config_revision_id: serverConfigRevisionId,
        dataset_none: true,
      },
    };
  }

  async function submitRunRequest(event) {
    event.preventDefault();
    if (runRequestState.submitting || !runRequestState.ready) return;
    const projectName = runRequestState.projectName;
    if (!projectName) return;
    const previewBox = element("pd-run-request-preview");
    const statusBox = element("pd-run-request-status");
    previewBox.hidden = true;
    previewBox.textContent = "";
    statusBox.textContent = "";
    const collected = runRequestCollectInputs();
    if (!collected.ok) {
      statusBox.textContent = collected.error;
      return;
    }
    runRequestState.submitting = true;
    runRequestSetControlsEnabled(false);
    statusBox.textContent = "正在呼叫 preview…";
    try {
      const draft = await api(
        `/projects/${encodeURIComponent(projectName)}/execution-plans/preview`,
        { method: "POST", body: JSON.stringify(collected.body) }
      );
      if (runRequestState.projectName !== projectName) return;
      if (!draft || !draft.ready) {
        const reasonText = runRequestReasonText(
          draft ? draft.reason_codes : [],
          draft ? draft.missing : []
        );
        previewBox.textContent = "Preview 顯示這個 Run 目前無法建立：\n" + reasonText;
        previewBox.hidden = false;
        statusBox.textContent = "尚未送出：請先解決上方 blocking reasons 後再試一次。";
        return;
      }
      previewBox.textContent =
        "Preview 通過（reproducible：" + (draft.reproducible ? "是" : "否") + "）。正在送出建立 Run 請求…";
      previewBox.hidden = false;
      const result = await api(
        `/projects/${encodeURIComponent(projectName)}/runs/request`,
        { method: "POST", body: JSON.stringify(collected.body) }
      );
      if (runRequestState.projectName !== projectName) return;
      previewBox.hidden = true;
      previewBox.textContent = "";
      const planId = result && result.plan ? result.plan.id : "unknown";
      statusBox.textContent =
        `已建立 pending approval #${result.approval_id}（plan_run，plan id：${planId}）；` +
        "請至「核准」頁核准後才會實際派發 Job。";
    } catch (error) {
      if (runRequestState.projectName !== projectName) return;
      statusBox.textContent = "建立 Run 請求失敗：" + String(error.message || error);
    } finally {
      if (runRequestState.projectName === projectName) {
        runRequestState.submitting = false;
        runRequestSetControlsEnabled(true);
      }
    }
  }

  // -------------------------------------------------------------------
  // Job results file list (PERSONAL_PILOT_PLAN §6 T2 / D3): read-only list
  // and per-file download for `results/{job_id}/`, plus a raw text preview
  // of `metrics.json` when it exists and is small enough. Reuses the log
  // slide-over panel (`#log-panel`) rather than a new surface — smallest
  // clean spot per the plan.
  // -------------------------------------------------------------------

  //: metrics.json 原文預覽上限（PLAN.md 慣例：其他結果檔案預覽同樣是
  //: 64 KiB，見 `app.main._CODING_RUN_FILE_MAX_CHARS`）。
  const JOB_RESULTS_METRICS_PREVIEW_MAX_BYTES = 65536;

  function jobResultsSetState(kind, message) {
    const box = element("log-results-state");
    if (!box) return;
    box.textContent = message;
    box.dataset.state = kind;
  }

  function resetJobResultsPanel() {
    const list = element("log-results-list");
    const metrics = element("log-results-metrics");
    if (list) list.replaceChildren();
    if (metrics) {
      metrics.hidden = true;
      metrics.textContent = "";
    }
    jobResultsSetState("loading", "正在載入結果檔案…");
  }

  function jobResultDownloadHref(jobId, path) {
    const segments = String(path)
      .split("/")
      .map((segment) => encodeURIComponent(segment));
    return `/jobs/${encodeURIComponent(jobId)}/results/${segments.join("/")}`;
  }

  async function fetchJobResultFileText(jobId, path) {
    const headers = {};
    if (typeof authToken !== "undefined" && authToken) {
      headers["X-Auth-Token"] = authToken;
    }
    const res = await fetch(jobResultDownloadHref(jobId, path), {
      credentials: "same-origin",
      headers,
    });
    if (!res.ok) throw new Error(`${res.status}: ${res.statusText}`);
    return res.text();
  }

  async function loadJobResultsPanel(jobId) {
    const list = element("log-results-list");
    const metrics = element("log-results-metrics");
    if (!list) return;
    list.replaceChildren();
    if (metrics) {
      metrics.hidden = true;
      metrics.textContent = "";
    }
    jobResultsSetState("loading", "正在載入結果檔案…");

    let data;
    try {
      data = await api(`/jobs/${jobId}/results`);
    } catch (error) {
      jobResultsSetState("error", "讀取結果檔案清單失敗：" + String(error.message || error));
      return;
    }

    // Missing directory is not an error — the worker may not have produced
    // any results, or the result-pull step may not have run yet.
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
    files.forEach((file) => {
      const item = document.createElement("li");
      const link = document.createElement("a");
      link.href = jobResultDownloadHref(jobId, file.path);
      // textContent only — file paths/sizes are server-supplied, untrusted text.
      link.textContent = `${file.path}（${file.size} bytes）`;
      item.append(link);
      list.append(item);
    });

    const metricsFile = files.find((file) => file.path === "metrics.json");
    if (
      metricsFile &&
      metrics &&
      typeof metricsFile.size === "number" &&
      metricsFile.size <= JOB_RESULTS_METRICS_PREVIEW_MAX_BYTES
    ) {
      try {
        const text = await fetchJobResultFileText(jobId, "metrics.json");
        metrics.textContent = text;
        metrics.hidden = false;
      } catch (error) {
        // metrics.json 預覽失敗不影響檔案清單本身；使用者仍可用上面的下載
        // 連結取得檔案。
        metrics.hidden = true;
      }
    }
  }

  window.DispatchUI = Object.freeze({
    initialize,
    updateIdentity,
    openEngineeringTaskWizard,
    closeEngineeringTaskWizard,
    renderStructuredInstruction,
    engineeringTaskDetailEnabled,
    loadEngineeringTasks,
    renderEngineeringTasks,
    clearEngineeringTasks,
    openEngineeringTaskDetail,
    closeEngineeringTaskDetail,
    parseProjectWorkspaceRoute,
    projectWorkspaceHash,
    activateProjectWorkspaceSection,
    resolveProjectWorkspaceRole,
    renderProjectWorkspaceRole,
    clearProjectWorkspaceRole,
    loadRunRequestPanel,
    resetRunRequestPanel,
    loadJobResultsPanel,
    resetJobResultsPanel,
  });
})();
