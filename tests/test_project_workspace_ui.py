"""Static contracts for the progressive Project workspace redesign.

Slice 6 deliberately keeps the checked-in, dependency-free HTML/CSS/JS
frontend and the existing project detail/timeline APIs.  These tests pin the
information architecture and presentation-only role behavior without needing
a browser runtime or granting the client authorization responsibilities.
"""

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "static" / "index.html"
UI_CSS = ROOT / "static" / "ui.css"
UI_JS = ROOT / "static" / "ui.js"

PROJECT_SECTIONS = (
    "overview",
    "code-version",
    "ai-engineering",
    "runs-validation",
    "data-artifacts",
    "settings",
    "deployment",
)

LEGACY_TABS = (
    "overview",
    "projects",
    "datasets",
    "jobs",
    "servers",
    "coding-runs",
    "chat",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def _opening_tag(source: str, tag: str, element_id: str) -> str:
    match = re.search(
        rf'<{re.escape(tag)}\b[^>]*\bid="{re.escape(element_id)}"[^>]*>', source
    )
    assert match is not None, f"missing <{tag}> with id={element_id}"
    return match.group(0)


def _javascript_function(source: str, name: str) -> str:
    """Return one named JS function while ignoring braces inside strings/comments."""

    match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match is not None, f"missing JavaScript function {name}"
    # ``match`` already ends at the function body's opening brace.  Searching
    # from the declaration start would incorrectly pick a destructured
    # parameter such as ``({ restoreFocus = true } = {})``.
    opening_brace = match.end() - 1
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    index = opening_brace
    while index < len(source):
        char = source[index]
        next_char = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char == "/" and next_char == "/":
            line_comment = True
            index += 1
        elif char == "/" and next_char == "*":
            block_comment = True
            index += 1
        elif char in {'"', "'", "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[match.start() : index + 1]
        index += 1
    raise AssertionError(f"unterminated JavaScript function {name}")


def _project_detail_markup() -> str:
    return _between(
        _read(INDEX_HTML),
        '<section id="tab-project-detail"',
        '<section id="tab-datasets"',
    )


def _workspace_pane(markup: str, section: str) -> str:
    start = markup.index(f'data-project-workspace-pane="{section}"')
    section_index = PROJECT_SECTIONS.index(section)
    if section_index + 1 < len(PROJECT_SECTIONS):
        end = markup.index(
            f'data-project-workspace-pane="{PROJECT_SECTIONS[section_index + 1]}"',
            start,
        )
    else:
        end = len(markup)
    return markup[start:end]


def test_project_workspace_has_fixed_accessible_subnavigation_and_seven_panes():
    markup = _project_detail_markup()
    css = _read(UI_CSS)

    assert 'id="project-subnavigation"' in markup
    assert re.search(
        r'<nav\b[^>]*id="project-subnavigation"[^>]*aria-label="[^"]*專案[^"]*"',
        markup,
    )
    for section in PROJECT_SECTIONS:
        assert markup.count(f'data-project-section="{section}"') == 1
        assert markup.count(f'data-project-workspace-pane="{section}"') == 1
        assert f'aria-controls="project-workspace-{section}"' in markup
        assert f'id="project-workspace-{section}"' in markup

    overview_control = re.search(
        r'<(?:a|button)\b[^>]*data-project-section="overview"[^>]*>', markup
    )
    assert overview_control is not None
    assert 'aria-current="page"' in overview_control.group(0)

    # The fixed subnavigation remains reachable on narrow screens by scrolling
    # inside the component rather than forcing a page-wide horizontal overflow.
    project_subnav_css = _between(css, ".project-subnav {", "}")
    assert "position: sticky" in project_subnav_css
    assert "overflow-x: auto" in css
    assert "@media (max-width: 768px)" in css


def test_workspace_places_material_actions_in_their_distinct_product_areas():
    markup = _project_detail_markup()
    overview = _workspace_pane(markup, "overview")
    code_version = _workspace_pane(markup, "code-version")
    ai_engineering = _workspace_pane(markup, "ai-engineering")
    runs_validation = _workspace_pane(markup, "runs-validation")
    data_artifacts = _workspace_pane(markup, "data-artifacts")
    settings = _workspace_pane(markup, "settings")
    deployment = _workspace_pane(markup, "deployment")

    assert 'id="pd-info-card"' in overview
    assert 'id="pd-timeline-list"' in overview
    assert "ProjectVersion" in code_version
    assert "instance" in code_version.lower() or "實例" in code_version

    assert 'id="pd-codex-btn"' in ai_engineering
    assert 'id="pd-dispatch-btn"' in runs_validation
    assert 'id="pd-probe-btn"' in runs_validation
    assert 'id="pd-data-artifacts"' in data_artifacts
    assert 'id="pd-delete-btn"' in settings
    assert 'id="pd-deploy-btn"' in deployment

    # A high-risk deployment action must not drift back beside ordinary run or
    # AI-modification actions.
    for unrelated_pane in (ai_engineering, runs_validation):
        assert 'id="pd-deploy-btn"' not in unrelated_pane
    assert 'class="danger' in deployment or 'data-risk="high"' in deployment


def test_workspace_extends_project_hash_routes_without_breaking_legacy_routes():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    combined = f"{index}\n{javascript}"

    # Every pre-Slice-6 top-level route and panel remains addressable.
    for tab in LEGACY_TABS:
        assert f'data-tab="{tab}"' in index
        assert f'id="tab-{tab}"' in index
        assert f'"{tab}"' in _between(index, "const KNOWN_TABS", ");")

    route_parser = _between(
        combined,
        "function parseProjectWorkspaceRoute(",
        "function projectWorkspaceHash(",
    )
    hash_builder = _between(
        combined,
        "function projectWorkspaceHash(",
        "function activateProjectWorkspaceSection(",
    )

    assert '.startsWith("#project/")' in route_parser
    assert 'section: "overview"' in route_parser
    assert "decodeURIComponent" in route_parser
    assert "PROJECT_WORKSPACE_SECTIONS" in route_parser
    assert '"#project/"' in hash_builder or "`#project/" in hash_builder
    assert "encodeURIComponent" in hash_builder

    # The old #project/<name> link is still emitted and means overview; the
    # optional section suffix only extends the contract.
    assert "#project/${encodeURIComponent(p.name)}" in index
    assert "parseProjectWorkspaceRoute" in _between(
        index, "function applyRoute()", "document.querySelectorAll(\"nav.tabs button\")"
    )
    assert 'document.getElementById("tab-project-detail")' in index
    assert 'b.dataset.tab === "projects"' in index


def test_section_switching_updates_navigation_and_panes_not_action_visibility():
    javascript = _read(UI_JS)
    switcher = _between(
        javascript,
        "function activateProjectWorkspaceSection(",
        "function resolveProjectWorkspaceRole(",
    )

    assert '"[data-project-section]"' in switcher
    assert '"[data-project-workspace-pane]"' in switcher
    assert 'setAttribute("aria-current", "page")' in switcher
    assert 'removeAttribute("aria-current")' in switcher
    assert ".hidden" in switcher
    for action_id in (
        "pd-codex-btn",
        "pd-dispatch-btn",
        "pd-probe-btn",
        "pd-delete-btn",
        "pd-deploy-btn",
    ):
        assert action_id not in switcher
    assert ".remove()" not in switcher


def test_project_role_badge_is_project_specific_and_presentation_only():
    markup = _project_detail_markup()
    javascript = _read(UI_JS)

    assert 'id="pd-role-badge"' in markup
    assert 'id="pd-role-guidance"' in markup
    resolver = _between(
        javascript,
        "function resolveProjectWorkspaceRole(",
        "function renderProjectWorkspaceRole(",
    )
    renderer = _between(
        javascript,
        "function renderProjectWorkspaceRole(",
        "function clearProjectWorkspaceRole(",
    )

    for required in (
        "authInfo.actor",
        "actor.platform_admin",
        'actor.type === "service"',
        "authInfo.project_memberships",
        "project_id",
        "project.id",
    ):
        assert required in resolver
    for role in ("Viewer", "Operator", "Project Admin", "Platform Admin", "Service actor"):
        assert role in javascript

    assert "textContent" in renderer
    assert ".innerHTML" not in renderer
    assert "PROJECT_WORKSPACE_ROLE_COPY" in renderer
    # Role-aware presentation is not authorization enforcement.  The renderer
    # must never locate, disable, remove, or hide any existing material action.
    for action_id in (
        "pd-codex-btn",
        "pd-dispatch-btn",
        "pd-probe-btn",
        "pd-delete-btn",
        "pd-deploy-btn",
    ):
        assert action_id not in renderer
    for forbidden in (".disabled", ".remove()", "style.display", "querySelectorAll"):
        assert forbidden not in renderer


def test_project_role_presentation_refreshes_from_auth_me_and_project_detail():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    identity_renderer = _between(
        javascript,
        "function updateIdentity(authInfo)",
        "function engineeringTaskDetailEnabled()",
    )
    project_loader = _between(
        index,
        "async function openProjectDetail(",
        "function renderProjectDetailInfo(detail)",
    )

    assert "state.currentAuthInfo = authInfo || null" in identity_renderer
    assert "renderProjectWorkspaceRole" in identity_renderer
    assert "renderProjectWorkspaceRole" in project_loader
    assert "detail.project" in project_loader
    assert "clearProjectWorkspaceRole" in _between(
        index, "function clearProtectedUi()", "function handleUnauthorizedResponse("
    )


def test_workspace_reads_server_returned_engineering_state_objects_by_code():
    index = _read(INDEX_HTML)
    normalizer = _javascript_function(index, "projectEngineeringPresentationValue")
    task_renderer = _javascript_function(index, "renderProjectEngineeringTasks")
    overview_renderer = _javascript_function(index, "renderProjectOverview")

    # The visibility API returns presentation.state/phase as {code, label}.
    # Never stringify that object into a CSS class or compare the object itself
    # with active-state strings.
    assert 'typeof value === "object"' in normalizer
    assert "value.code" in normalizer
    assert "value.label" in normalizer
    assert 'projectEngineeringPresentationValue(task, "state"' in task_renderer
    assert "taskState.code" in task_renderer
    assert "taskState.label" in task_renderer
    assert 'projectEngineeringPresentationValue(task, "state"' in overview_renderer
    assert ".code" in overview_renderer


def test_workspace_deduplicates_native_validation_requests_and_withholds_commands():
    index = _read(INDEX_HTML)
    request_id = _javascript_function(index, "nativeEngineeringValidationRequestId")
    counter = _javascript_function(index, "nativeEngineeringValidationCount")
    presentation = _javascript_function(index, "projectJobPresentation")
    renderer = _javascript_function(index, "renderProjectRuns")

    assert "job.engineering_validation_request_id" in request_id
    assert 'return value == null || value === "" ? null : String(value)' in request_id
    assert "new Set(" in counter
    assert ".map(nativeEngineeringValidationRequestId)" in counter
    assert '.filter((requestId) => requestId !== null)' in counter
    assert "nativeEngineeringValidationCount(projectJobs)" in renderer
    assert 'job.type === "validation"' not in renderer

    assert "if (validationRequestId !== null)" in presentation
    assert 'job.type === "sync"' in presentation
    assert 'job.type === "adhoc"' in presentation
    assert '"Worker validation staging"' in presentation
    assert '"Worker validation execution"' in presentation
    assert '"Worker validation step"' in presentation
    assert 'commandLabel: "受核准的驗證步驟（執行內容不公開）"' in presentation
    assert presentation.count("job.command") == 1
    assert presentation.index('commandLabel: "受核准的驗證步驟') < presentation.index(
        "job.command"
    )
    assert "job.command" not in renderer
    assert "presentation.kindLabel" in renderer
    assert "presentation.commandLabel" in renderer


def test_projects_page_summaries_use_only_existing_authenticated_evidence():
    index = _read(INDEX_HTML)
    renderer = _javascript_function(index, "renderProjects")
    loader = _javascript_function(index, "loadProjectsMatrix")
    role_renderer = _javascript_function(index, "projectRoleSummaryHtml")
    version_renderer = _javascript_function(index, "projectLatestVersionSummaryHtml")
    engineering_state = _javascript_function(index, "projectEngineeringState")

    for label in (
        "Project role",
        "Latest immutable ProjectVersion",
        "Instance health",
        "Active AI tasks",
        "Active Jobs",
    ):
        assert label in renderer

    assert 'api("/projects/matrix")' in loader
    assert 'api("/engineering-tasks?limit=100")' in loader
    assert "/projects/${encodeURIComponent(name)}/versions" in loader
    assert "/activity" not in loader
    assert "Promise.all" in loader

    assert "resolveProjectWorkspaceRole" in role_renderer
    for forbidden in (".disabled", ".hidden", ".remove()"):
        assert forbidden not in role_renderer

    # Hub HEAD is not substituted for a missing immutable ProjectVersion.
    assert "projectVersionsSummaryCache" in version_renderer
    assert "latest.git_commit" in version_renderer
    assert "hub" not in version_renderer.lower()
    assert "尚無 ProjectVersion" in version_renderer

    assert 'typeof presentationState === "string"' in engineering_state
    assert "presentationState.code" in engineering_state


def test_workspace_reuses_existing_project_apis_and_does_not_add_frontend_dependencies():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    # Workspace navigation is a presentation change over the existing detail
    # and timeline resources, not a parallel project backend.
    project_loader = _between(
        index,
        "async function openProjectDetail(",
        "function renderProjectDetailInfo(detail)",
    )
    assert "/detail`" in project_loader
    assert "/timeline?limit=20`" in project_loader
    assert "/project-workspace" not in index
    assert "/project-workspace" not in javascript

    frontend_manifests = (
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "vite.config.js",
        "webpack.config.js",
    )
    for manifest in frontend_manifests:
        assert not (ROOT / manifest).exists()

    scripts = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', index)
    stylesheets = re.findall(
        r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="([^"]+)"', index
    )
    assert len(scripts) == 1
    assert len(stylesheets) == 1
    assert re.fullmatch(r"/static/ui\.js\?v=[A-Za-z0-9._-]+", scripts[0])
    assert re.fullmatch(r"/static/ui\.css\?v=[A-Za-z0-9._-]+", stylesheets[0])
    assert "https://" not in index
    for forbidden in (
        "import ",
        "require(",
        "node_modules",
        "React.",
        "Vue.",
        "createApp(",
    ):
        assert forbidden not in javascript


def test_mobile_navigation_is_an_accessible_modal_drawer_with_focus_lifecycle():
    javascript = _read(UI_JS)
    opener = _javascript_function(javascript, "openNavigation")
    closer = _javascript_function(javascript, "closeNavigation")
    synchronizer = _javascript_function(javascript, "syncNavigationAccessibility")
    focus_trap = _javascript_function(javascript, "trapNavigationFocus")
    initializer = _javascript_function(javascript, "initialize")

    assert 'classList.add("navigation-open")' in opener
    assert 'setAttribute("aria-expanded", "true")' in opener
    assert "syncNavigationAccessibility()" in opener
    assert 'element("mobile-nav-close-btn").focus()' in opener

    assert 'classList.remove("navigation-open")' in closer
    assert 'setAttribute("aria-expanded", "false")' in closer
    assert "frame.inert = false" in closer
    assert 'frame.removeAttribute("aria-hidden")' in closer
    assert "focusWasInside" in closer
    assert 'element("mobile-nav-open-btn").focus()' in closer

    assert "navigation.inert = mobile && !open" in synchronizer
    assert 'navigation.setAttribute("aria-hidden", "true")' in synchronizer
    assert 'navigation.removeAttribute("aria-hidden")' in synchronizer
    assert "frame.inert = open" in synchronizer
    assert 'frame.setAttribute("aria-hidden", "true")' in synchronizer
    assert 'frame.removeAttribute("aria-hidden")' in synchronizer

    assert 'event.key !== "Tab"' in focus_trap
    assert 'classList.contains("navigation-open")' in focus_trap
    assert "navigation.querySelectorAll" in focus_trap
    assert "document.activeElement === first" in focus_trap
    assert "document.activeElement === last" in focus_trap
    assert focus_trap.count("event.preventDefault()") >= 2

    assert 'document.addEventListener("keydown"' in initializer
    assert "trapNavigationFocus(event)" in initializer
    assert 'event.key === "Escape"' in initializer
    assert "closeNavigation({ restoreFocus: true })" in initializer


def test_runtime_record_and_deployment_modals_have_dialog_semantics():
    index = _read(INDEX_HTML)
    for modal_id, title_id in (
        ("dispatch-modal", "dispatch-modal-title"),
        ("record-modal", "record-modal-title"),
        ("project-deploy-modal", "project-deploy-modal-title"),
    ):
        tag = _opening_tag(index, "div", modal_id)
        assert 'role="dialog"' in tag
        assert 'aria-modal="true"' in tag
        assert f'aria-labelledby="{title_id}"' in tag
        assert f'id="{title_id}"' in index


def test_legacy_modal_manager_traps_focus_handles_escape_and_restores_opener():
    index = _read(INDEX_HTML)
    opener = _javascript_function(index, "openLegacyModal")
    closer = _javascript_function(index, "closeAllModals")
    manager = _between(
        index,
        "let activeLegacyModal",
        "// ---- 新增專案彈窗",
    )
    key_handler = _between(
        manager,
        'document.addEventListener("keydown", (event) => {',
        "\n});",
    )

    assert "document.activeElement" in opener
    assert "legacyModalOpener" in opener
    assert 'classList.add("open")' in opener
    assert 'classList.add("dialog-open")' in opener
    assert ".focus()" in opener

    assert "restoreFocus" in closer
    assert "legacyModalOpener" in closer
    assert "legacyModalRestoreTarget(opener)" in closer
    assert ".focus(" in closer
    assert 'classList.remove("dialog-open")' in closer

    assert "activeLegacyModal" in key_handler
    assert 'classList.contains("open")' in key_handler
    assert 'event.key === "Escape"' in key_handler
    assert "closeAllModals({ restoreFocus: true })" in key_handler
    assert 'event.key !== "Tab"' in key_handler
    assert "document.activeElement === first" in key_handler
    assert "document.activeElement === last" in key_handler
    assert key_handler.count("event.preventDefault()") >= 3

    assert 'document.addEventListener("keydown", (event) => {' in manager

    dispatch_opener = _between(
        index, "window.openDispatchModal = function", "function serverPlacementLabel("
    )
    deploy_opener = _between(
        index,
        "window.openProjectDeployModal = function",
        'document.getElementById("project-deploy-cancel-btn")',
    )
    record_opener = _between(
        index,
        "window.openRecordModal = function",
        'document.getElementById("pd-add-record-btn")',
    )
    assert 'openLegacyModal("dispatch-modal")' in dispatch_opener
    assert 'openLegacyModal("project-deploy-modal")' in deploy_opener
    assert 'openLegacyModal("record-modal")' in record_opener


def test_timeline_cards_are_semantic_articles_with_explicit_action_controls():
    index = _read(INDEX_HTML)
    renderer = _javascript_function(index, "timelineItemHtml")

    assert renderer.count('<article class="card timeline-card') == 3
    assert not re.search(r'<div\b[^>]*class="[^"]*timeline-card', renderer)
    assert 'class="card timeline-card clickable"' not in renderer
    assert 'role="button"' not in renderer
    clickable_tags = re.findall(r"<[^>]+\bonclick=\"[^\"]+\"[^>]*>", renderer)
    assert clickable_tags
    assert all(tag.lstrip().startswith("<button") for tag in clickable_tags)
    assert 'aria-label="查看任務 #' in renderer
    assert 'aria-label="查看 Coding Run #' in renderer


def test_project_settings_textareas_and_actions_have_specific_accessible_names():
    settings = _workspace_pane(_project_detail_markup(), "settings")
    field_names = {
        "goal": "專案目標",
        "optimization_notes": "優化方法",
        "progress": "目前進度",
    }
    for field, label in field_names.items():
        assert f'<label class="visually-hidden" for="pd-doc-textarea-{field}">' in settings
        assert f'<textarea id="pd-doc-textarea-{field}"' in settings
        assert f'aria-label="編輯{label}"' in settings
        assert f'aria-label="儲存{label}"' in settings
        assert f'aria-label="取消編輯{label}"' in settings
    assert re.search(r'<button\b[^>]*id="pd-delete-btn"[^>]*>刪除專案登記</button>', settings)


def test_project_settings_surfaces_only_existing_approval_gated_membership_contract():
    index = _read(INDEX_HTML)
    settings = _workspace_pane(_project_detail_markup(), "settings")

    for element_id in (
        "pd-membership-state",
        "pd-membership-content",
        "pd-membership-list",
        "pd-membership-form",
        "pd-membership-actor-id",
        "pd-membership-role",
        "pd-membership-submit-btn",
        "pd-membership-refresh-btn",
    ):
        assert f'id="{element_id}"' in settings
    assert 'for="pd-membership-actor-id"' in settings
    assert 'for="pd-membership-role"' in settings
    assert 'value="viewer"' in settings
    assert 'value="operator"' in settings
    assert 'value="admin"' in settings
    assert "pending approval" in settings
    assert "service actor" in settings.lower()

    loader = _javascript_function(index, "loadProjectMemberships")
    submitter = _javascript_function(index, "submitProjectMembershipMutation")
    normalizer = _javascript_function(index, "normalizeProjectMemberships")
    assert '`/projects/${encodeURIComponent(name)}/memberships`' in loader
    assert 'identity administration is disabled' in loader
    assert 'row.project_id !== project.id' in normalizer
    assert 'row.project !== project.name' in normalizer
    assert 'PROJECT_MEMBERSHIP_ROLES.has(role)' in normalizer
    assert 'actor.id !== actorId' in normalizer
    assert 'actor.email' not in normalizer
    assert 'actor.subject' not in normalizer
    assert '/memberships/request`' in submitter
    assert '/remove-request`' in submitter
    assert 'JSON.stringify({ actor_id: actorId, role })' in submitter
    assert 'approval.status !== "pending"' in _javascript_function(
        index, "membershipApprovalId"
    )
    for forbidden in ("/approve/", "raw_token", "secret_hash", "password"):
        assert forbidden not in submitter


def test_membership_ui_fails_closed_and_is_not_role_badge_authorization():
    index = _read(INDEX_HTML)
    loader = _javascript_function(index, "loadProjectMemberships")
    state_setter = _javascript_function(index, "setProjectMembershipState")
    control_setter = _javascript_function(index, "setProjectMembershipControlsEnabled")
    submitter = _javascript_function(index, "submitProjectMembershipMutation")

    for state in ("loading", "empty", "error", "disconnected"):
        assert state in index
    assert "projectMembershipLoadSerial" in loader
    assert "projectDetailOpenSerial" in loader
    assert "projectDetailCurrent !== name" in loader
    assert "content.hidden = true" in state_setter
    assert "setProjectMembershipControlsEnabled(false)" in state_setter
    assert ".disabled = !allow" in control_setter
    assert "projectMembershipMutationSerial" in submitter
    assert "projectDetailOpenSerial" in submitter
    assert "projectMembershipState !== \"ready\"" in submitter
    for role_signal in ("platform_admin", "project_memberships", "actor.type"):
        assert role_signal not in control_setter
        assert role_signal not in submitter


def test_mobile_css_never_removes_task_status_description_or_action_explanation():
    index = _read(INDEX_HTML)
    css = _read(UI_CSS)
    mobile_css = css[css.index("@media (max-width: 768px)") :]
    protected_selectors = (
        "#engineering-task-detail-status",
        "#engineering-task-detail-description",
        "#engineering-task-action-note",
        ".task-detail-header-actions",
        ".task-detail-action-bar",
    )

    for element_id in (
        "engineering-task-detail-status",
        "engineering-task-detail-description",
        "engineering-task-action-note",
    ):
        assert f'id="{element_id}"' in index
        assert not re.search(
            rf'id="{re.escape(element_id)}"[^>]*\bhidden(?:\s|>|=)', index
        )

    for selector in protected_selectors:
        selector_rules = re.findall(
            rf"[^{{}}]*{re.escape(selector)}[^{{}}]*\{{([^{{}}]*)\}}",
            mobile_css,
        )
        assert all("display: none" not in body for body in selector_rules)


def test_mobile_task_detail_contains_long_values_without_global_clipping():
    css = _read(UI_CSS)
    body_rule = _between(css, "body {", "}")
    header_copy = _between(css, ".ui-dialog-header > div:first-child {", "}")
    title = _between(css, "#engineering-task-detail-title {", "}")
    action_bar = _between(css, ".task-detail-action-bar {", "}")

    assert "overflow-x: hidden" not in body_rule
    assert "min-width: 0" in header_copy
    assert "overflow-wrap: anywhere" in title
    assert "word-break: break-word" in title
    assert ".task-test > code" in css
    assert ".task-artifact > strong" in css
    assert ".task-artifact-meta" in css
    assert "overflow-wrap: anywhere" in css
    assert "overflow-x: auto" in action_bar


def test_project_settings_run_profile_and_policy_managers_are_approval_gated():
    """Goal 2 UI（Goal 3 Stage 1）：Run Profile 與調度政策管理器完全比照
    membership manager 模式——載入前停用、feature flag 關閉顯示未啟用、
    所有寫入只打 *-request 端點建立 pending approval，絕不直接 /approve/。"""

    index = _read(INDEX_HTML)
    settings = _workspace_pane(_project_detail_markup(), "settings")

    for element_id in (
        "pd-runprofile-state",
        "pd-runprofile-content",
        "pd-runprofile-list",
        "pd-runprofile-form",
        "pd-runprofile-name",
        "pd-runprofile-command",
        "pd-runprofile-submit-btn",
        "pd-runprofile-refresh-btn",
        "pd-runprofile-cancel-edit-btn",
        "pd-policy-state",
        "pd-policy-content",
        "pd-policy-list",
        "pd-policy-form",
        "pd-policy-name",
        "pd-policy-servers",
        "pd-policy-runprofile",
        "pd-policy-max",
        "pd-policy-valid-until",
        "pd-policy-dataset-required",
        "pd-policy-submit-btn",
        "pd-policy-refresh-btn",
    ):
        assert f'id="{element_id}"' in settings
    for label_for in (
        "pd-runprofile-name",
        "pd-runprofile-command",
        "pd-policy-name",
        "pd-policy-servers",
        "pd-policy-max",
    ):
        assert f'for="{label_for}"' in settings
    assert "pending approval" in settings
    # 送出前控制項一律 disabled；能力未確認前不得看起來可用。
    for control_id in ("pd-runprofile-name", "pd-policy-name", "pd-policy-servers"):
        opening = re.search(rf'<input\b[^>]*\bid="{control_id}"[^>]*>', settings)
        assert opening is not None and "disabled" in opening.group(0)

    factory = _javascript_function(index, "createPdApprovalManager")
    # fail-closed 契約：serial 防護、非 ready 不送出、狀態切換時停用控制項。
    assert "projectDetailOpenSerial" in factory
    assert "manager.loadSerial" in factory
    assert 'manager.state !== "ready"' in factory
    assert "setControlsEnabled(false)" in factory
    assert "projectRequestDisconnected" in factory
    assert "/approve/" not in factory
    # 兩個實例只打 request／update-request／archive-request 端點。
    assert "/run-profiles/request`" in index
    assert "/run-profiles/${encodeURIComponent(profile)}/update-request`" in index
    assert "/run-profiles/${encodeURIComponent(profile)}/archive-request`" in index
    assert "/dispatch-policies/request`" in index
    assert "/dispatch-policies/${encodeURIComponent(policy)}/update-request`" in index
    assert "/dispatch-policies/${encodeURIComponent(policy)}/archive-request`" in index
    # flag 關閉時的誠實訊息（後端 404 detail 原文比對）。
    assert "Run Profile administration is disabled" in index
    assert "Dispatch Policy administration is disabled" in index
    # 政策表單的 Run Profile 選項只列 approved head。
    options_renderer = _javascript_function(index, "renderPdPolicyRunProfileOptions")
    assert 'profile.status !== "approved"' in options_renderer


def test_run_request_form_previews_before_submit_and_pins_dataset_none():
    """PERSONAL_PILOT_PLAN §6 T1：執行與驗證分頁新增「建立 Run」表單——
    promoted version／目標下拉、command、固定 dataset_none，且一律先呼叫
    既有 preview 端點才送出 `runs/request`，從不直接打 `/approve/`。"""

    index = _read(INDEX_HTML)
    runs_pane = _workspace_pane(_project_detail_markup(), "runs-validation")

    for element_id in (
        "pd-run-request-state",
        "pd-run-request-form",
        "pd-run-request-version",
        "pd-run-request-target",
        "pd-run-request-command",
        "pd-run-request-submit-btn",
        "pd-run-request-refresh-btn",
        "pd-run-request-preview",
        "pd-run-request-status",
    ):
        assert f'id="{element_id}"' in runs_pane

    for label_for in ("pd-run-request-version", "pd-run-request-target", "pd-run-request-command"):
        assert f'for="{label_for}"' in runs_pane

    # 送出前控制項一律 disabled；JS 只在版本／目標都可用時才啟用。
    for tag, control_id in (
        ("select", "pd-run-request-version"),
        ("select", "pd-run-request-target"),
        ("textarea", "pd-run-request-command"),
    ):
        opening = _opening_tag(runs_pane, tag, control_id)
        assert "disabled" in opening
    submit_opening = re.search(
        r'<button\b[^>]*\bid="pd-run-request-submit-btn"[^>]*>', runs_pane
    )
    assert submit_opening is not None and "disabled" in submit_opening.group(0)

    # dataset_none 固定為 true；表單不提供資料集挑選欄位，也不出現
    # require_reproducible 輸入。
    assert "dataset_none" in runs_pane
    assert 'id="pd-run-request-dataset' not in runs_pane
    assert 'name="require_reproducible"' not in runs_pane

    javascript = _read(UI_JS)
    loader = _javascript_function(javascript, "loadRunRequestPanel")
    assert "/versions`" in loader
    assert "server_config_revision_id" in loader

    submitter = _javascript_function(javascript, "submitRunRequest")
    # Preview 一定先於 request；未 ready 時絕不送出 runs/request。
    preview_at = submitter.index("execution-plans/preview")
    request_at = submitter.index("runs/request")
    assert preview_at < request_at
    assert "draft.ready" in submitter
    assert "/approve/" not in submitter
    assert "/approve/" not in loader

    collector = _javascript_function(javascript, "runRequestCollectInputs")
    assert "dataset_none: true" in collector


def test_kind_label_covers_plan_run_and_engineering_task_promote():
    """新的 plan_run（`runs/request` 建立的 pending approval）與既有的
    engineering_task_promote 都要有可讀的 zh-TW 標籤，不能落到原始 kind
    字串（generic approvals 卡片就是照 `KIND_LABEL[a.kind]` 顯示）。"""

    index = _read(INDEX_HTML)
    kind_label = _between(index, "const KIND_LABEL = {", "};")
    assert "plan_run:" in kind_label
    assert "engineering_task_promote:" in kind_label


def test_run_request_version_dropdown_filters_to_promoted_versions():
    """PERSONAL_PILOT_PLAN §6 T2 carry-over: `GET /projects/{name}/versions`
    now carries `promotion_state`, so the T1 「建立 Run」 dropdown filters
    down to `promotion_state === "promoted"` instead of listing every
    ProjectVersion and asking the requester to self-verify by hand."""

    javascript = _read(UI_JS)
    loader = _javascript_function(javascript, "loadRunRequestPanel")

    assert 'promotion_state === "promoted"' in loader
    assert "尚無已發布版本，請先完成 AI 工程任務並正式發布。" in loader
    # promotedVersions (not the raw unfiltered list) gates readiness and
    # populates the <select>.
    assert "promotedVersions.length > 0" in loader
    assert "promotedVersions.forEach" in loader


def test_job_results_panel_lists_files_and_previews_metrics_json():
    """PERSONAL_PILOT_PLAN §6 T2 / D3: the job log slide-over panel gets a
    read-only results file list (with per-file download links) and an
    inline raw-text preview of `metrics.json` when it exists and is small
    enough. Only textContent is ever assigned from server-supplied values —
    no innerHTML with response data."""

    index = _read(INDEX_HTML)
    log_panel = _between(index, '<div id="log-panel"', '<div id="coding-run-panel"')
    for element_id in (
        "log-results-section",
        "log-results-state",
        "log-results-list",
        "log-results-metrics",
    ):
        assert f'id="{element_id}"' in log_panel

    open_log = _between(index, "window.openLog = async function (id) {", "};")
    assert "resetJobResultsPanel" in open_log
    assert "loadJobResultsPanel" in open_log

    javascript = _read(UI_JS)
    loader = _javascript_function(javascript, "loadJobResultsPanel")
    assert "/jobs/${jobId}/results" in loader
    assert "data.collected === false" in loader
    assert "尚未收集到結果檔案" in loader
    assert "jobResultDownloadHref" in loader
    assert 'file.path === "metrics.json"' in loader
    assert "JOB_RESULTS_METRICS_PREVIEW_MAX_BYTES" in loader
    assert "metrics.textContent" in loader
    assert "metrics.innerHTML" not in loader
    assert "link.innerHTML" not in loader

    href_builder = _javascript_function(javascript, "jobResultDownloadHref")
    assert "encodeURIComponent" in href_builder
    assert ".split(" in href_builder

    link_builder_source = loader
    assert "link.textContent" in link_builder_source
    assert "link.innerHTML" not in link_builder_source
