"""Source-level UI foundation and AI Engineering Task wizard contracts.

The frontend remains dependency-free vanilla HTML/CSS/JS.  These tests pin the
security- and compatibility-sensitive wiring without adding a browser runtime.
"""

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "static" / "index.html"
UI_CSS = ROOT / "static" / "ui.css"
UI_JS = ROOT / "static" / "ui.js"
ENGINEERING_TASKS_PY = ROOT / "app" / "engineering_tasks.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def _css_hex_token(css: str, name: str) -> str:
    marker = f"{name}: #"
    start = css.index(marker) + len(name) + 2
    return css[start : css.index(";", start)].strip()


def _contrast_ratio(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        value = value.removeprefix("#")
        channels = [int(value[index : index + 2], 16) / 255 for index in (0, 2, 4)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_ui_assets_exist_are_cache_busted_and_dependency_free():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    assert 'href="/static/ui.css?v=20260715-13"' in index
    assert 'src="/static/ui.js?v=20260715-13"' in index
    assert index.index("ui.css?v=20260715-13") < index.index("</head>")
    assert index.index("ui.js?v=20260715-13") < index.index('"use strict";')
    assert javascript.startswith("(function () {\n  \"use strict\";")
    assert javascript.rstrip().endswith("})();")
    for forbidden in ("import ", "require(", "node_modules", "package.json"):
        assert forbidden not in javascript


def test_application_shell_is_semantic_and_preserves_every_hash_route():
    index = _read(INDEX_HTML)

    assert '<a class="skip-link" href="#main-content">' in index
    assert '<aside id="primary-navigation"' in index
    assert '<nav class="tabs" aria-label="平台區域">' in index
    assert '<main id="main-content" tabindex="-1">' in index
    assert 'id="page-breadcrumb"' in index
    assert 'id="page-title"' in index
    assert 'id="mobile-nav-open-btn"' in index
    assert 'aria-controls="primary-navigation"' in index
    assert 'id="navigation-scrim"' in index

    for route in (
        "overview",
        "projects",
        "datasets",
        "jobs",
        "servers",
        "coding-runs",
        "chat",
    ):
        assert f'data-tab="{route}"' in index
        assert f'id="tab-{route}"' in index

    primary_nav = _between(index, '<nav class="tabs" aria-label="平台區域">', "</nav>")
    assert primary_nav.count("<button ") == 9
    for route, label in (
        ("overview", "總覽"),
        ("projects", "專案"),
        ("work", "工作"),
        ("approvals", "核准"),
        ("datasets", "資料與結果"),
        ("servers", "基礎設施"),
        ("activity", "活動與稽核"),
        ("administration", "管理"),
        ("chat", "助手"),
    ):
        assert f'data-tab="{route}"' in primary_nav
        assert f"<span>{label}</span>" in primary_nav
    assert 'data-work-route="coding-runs"' in index


def test_design_tokens_responsive_breakpoints_and_accessibility_foundation():
    css = _read(UI_CSS)

    for token in (
        "--ui-canvas",
        "--ui-surface",
        "--ui-border",
        "--ui-text",
        "--ui-muted",
        "--ui-focus",
        "--ui-success",
        "--ui-warning",
        "--ui-danger",
        "--ui-safety-rejected",
        "--ui-pending-approval",
        "--ui-queued",
        "--ui-unknown",
        "--ui-interrupted",
        "--ui-disconnected",
        "--ui-cancelled",
    ):
        assert token in css
    assert ":focus-visible" in css
    assert "min-height: 44px" in css
    body_rule = _between(css, "body {", "}")
    assert "overflow-x: hidden" not in body_rule
    assert "main table" in css and "overflow-x: auto" in css
    assert "word-break: normal" in css
    assert "@media (max-width: 1024px)" in css
    assert "@media (max-width: 768px)" in css
    assert "@media (max-width: 480px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_statuses_have_distinct_textual_or_symbolic_treatments():
    css = _read(UI_CSS)
    javascript = _read(UI_JS)

    for status in ("unknown", "interrupted", "disconnected", "failed"):
        assert status in css or status in javascript
    assert 'title: "Runner 狀態 disconnected"' in javascript
    assert 'title: `${status.server || "Coding Runner"} 離線`' in javascript
    assert 'status.probe_status === "probe_failed"' in javascript
    assert 'title: "Runner 能力探測失敗"' in javascript
    assert 'title: "Runner 未安裝 Codex"' in javascript
    assert 'title: "Codex 尚未登入"' in javascript
    assert 'kind: "warning"' in javascript
    assert 'busy' in javascript


def test_status_pills_meet_wcag_aa_and_keep_non_color_distinctions():
    index = _read(INDEX_HTML)
    css = _read(UI_CSS)

    assert index.index("</style>") < index.index('href="/static/ui.css')
    assert "color: #fff" in _between(index, ".status-pill {", "}")
    assert ".status-queued { background: var(--ui-queued); }" in css
    assert ".status-cancelled { background: var(--ui-cancelled); }" in css
    assert ".status-unknown { background: var(--ui-unknown); }" in css
    assert ".status-interrupted { background: var(--ui-interrupted); }" in css
    assert ".status-disconnected { background: var(--ui-disconnected); }" in css
    assert ".status-pending_approval { background: var(--ui-pending-approval); }" in css
    assert ".status-secret_violation { background: var(--ui-safety-rejected); }" in css
    assert ".status-failed { background: var(--red); }" in index
    assert "--red: var(--ui-danger);" in css

    backgrounds = {
        status: _css_hex_token(css, token)
        for status, token in {
            "queued": "--ui-queued",
            "cancelled": "--ui-cancelled",
            "unknown": "--ui-unknown",
            "interrupted": "--ui-interrupted",
            "disconnected": "--ui-disconnected",
            "pending_approval": "--ui-pending-approval",
            "secret_violation": "--ui-safety-rejected",
            "failed": "--ui-danger",
        }.items()
    }
    assert len(set(backgrounds.values())) == len(backgrounds)
    for background in backgrounds.values():
        assert _contrast_ratio("#ffffff", background) >= 4.5

    for selector, symbol in {
        ".status-queued::before": "…",
        ".status-cancelled::before": "—",
        ".status-unknown::before": "?",
        ".status-interrupted::before": "◇",
        ".status-disconnected::before": "↯",
        ".status-pending_approval::before": "⌛",
        ".status-secret_violation::before": "⛨",
        ".status-failed::before": "×",
    }.items():
        assert f'{selector} {{ content: "{symbol}"; }}' in css


def test_wizard_dialog_has_accessible_name_five_steps_and_focus_management():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    dialog = _between(index, 'id="engineering-task-dialog"', '<div id="project-modal"')
    assert 'role="dialog"' in dialog
    assert 'aria-modal="true"' in dialog
    assert 'aria-labelledby="engineering-task-title"' in dialog
    assert 'aria-describedby="engineering-task-description"' in dialog
    assert 'id="engineering-task-title" tabindex="-1"' in dialog
    assert dialog.count('data-wizard-page="') == 5
    assert dialog.count('data-step="') == 5
    assert 'id="engineering-objective-error" class="field-error" hidden' in dialog
    assert 'aria-describedby="engineering-objective-help engineering-objective-error"' in dialog

    assert 'event.key === "Escape"' in javascript
    assert 'event.key !== "Tab"' in javascript
    assert "state.opener = document.activeElement" in javascript
    assert "opener.focus()" in javascript
    assert 'element("engineering-task-title").focus()' in javascript


def test_legacy_modal_and_handler_remain_behind_static_feature_flag():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    assert 'AI_ENGINEERING_TASK_UI_V2: true' in index
    assert 'id="coding-task-modal" class="modal"' in index
    assert 'id="coding-task-instruction"' in index
    assert 'id="coding-task-submit-btn"' in index
    assert 'document.getElementById("coding-task-submit-btn").addEventListener' in index
    assert "state.legacyOpenCodingTaskModal = window.openCodingTaskModal" in javascript
    assert "if (!flags.AI_ENGINEERING_TASK_UI_V2)" in javascript
    assert "state.legacyOpenCodingTaskModal(projectName)" in javascript
    assert "window.openCodingTaskModal = openEngineeringTaskWizard" in javascript


def test_structured_instruction_renderer_has_fixed_section_order_and_bullets():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    backend = _read(ENGINEERING_TASKS_PY)
    renderer = _between(
        javascript,
        "function renderStructuredInstruction(",
        "function readFormValues()",
    )
    backend_renderer = _between(
        backend,
        "def render_engineering_task_instruction(",
        "def detect_project_metadata(",
    )
    headings = (
        "AI Engineering Task",
        "Task objective",
        "Background and relevant context",
        "Expected changes",
        "Non-goals",
        "Allowed modification scope",
        "Prohibited paths",
        "Prohibited changes",
        "Acceptance criteria",
        "Validation strategy",
        "Requested execution behavior",
    )
    positions = [renderer.index(heading) for heading in headings]
    assert positions == sorted(positions)
    backend_positions = [backend_renderer.index(heading) for heading in headings]
    assert backend_positions == sorted(backend_positions)
    assert 'map((item) => `- ${item}`)' in javascript
    assert '.map((item) => item.trim())' in javascript
    assert ".filter(Boolean)" in javascript
    assert 'return parts.join("\\n\\n")' in renderer
    assert 'id="engineering-non-goals"' in index
    assert 'addSection(parts, "Non-goals", bulletItems(values.nonGoals));' in renderer
    assert 'nonGoals: element("engineering-non-goals").value.trim()' in javascript


def test_wizard_validation_copy_and_renderer_match_sandboxed_backend_contract():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    backend = _read(ENGINEERING_TASKS_PY)
    renderer = _between(
        javascript,
        "function renderStructuredInstruction(",
        "function readFormValues()",
    )
    backend_tree = ast.parse(backend)
    backend_renderer = next(
        node
        for node in backend_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "render_engineering_task_instruction"
    )
    backend_literals = {
        node.value
        for node in ast.walk(backend_renderer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    tests_instruction = (
        "Run the repository's relevant tests and lint checks only inside this "
        "current sandboxed agent turn; the outer Runner will not execute "
        "repository code after the turn."
    )
    build_instruction = (
        "Run relevant build and smoke checks only inside this current "
        "sandboxed agent turn."
    )

    assert "仍使用現有猜測式驗證" not in index
    assert "只要求在目前 sandboxed agent turn 內執行" in index
    assert "外層 Runner 不會在回合結束後執行 repository code" in index
    assert "外層 Runner 不會代為執行" in index
    assert f'"{tests_instruction}"' in renderer
    assert f'"{build_instruction}"' in renderer
    assert tests_instruction in backend_literals
    assert build_instruction in backend_literals
    assert renderer.index("Validation strategy") < renderer.index(
        "Requested execution behavior"
    )
    validation_part = renderer[
        renderer.index("const validation = [];") : renderer.index(
            'addSection(parts, "Validation strategy", validation);'
        )
    ]
    assert "worker validation preference" in validation_part
    execution_part = renderer[
        renderer.index("const execution = [") : renderer.index(
            'addSection(parts, "Requested execution behavior", execution);'
        )
    ]
    assert "worker validation preference" not in execution_part


def test_preview_character_count_matches_backend_unicode_code_point_limit():
    javascript = _read(UI_JS)
    backend = _read(ENGINEERING_TASKS_PY)
    counter = _between(javascript, "function codePointLength(", "function bulletItems(")
    validator = _between(
        javascript,
        "function validateAll(",
        "function updatePreview()",
    )
    preview = _between(javascript, "function updatePreview()", "function showStep(")

    assert "ENGINEERING_TASK_INSTRUCTION_LIMIT = 4000" in backend
    assert "const INSTRUCTION_LIMIT = 4000" in javascript
    assert 'return Array.from(String(value || "")).length;' in counter
    assert "if len(instruction) > ENGINEERING_TASK_INSTRUCTION_LIMIT:" in backend
    assert "codePointLength(instruction) > INSTRUCTION_LIMIT" in validator
    assert "const count = codePointLength(instruction);" in preview
    assert "count <= INSTRUCTION_LIMIT" in preview


def test_wizard_has_immutable_and_exact_legacy_submission_paths():
    javascript = _read(UI_JS)
    submitter = _between(
        javascript,
        "async function submitEngineeringTask(event)",
        "function focusableElements(container)",
    )

    assert "const INSTRUCTION_LIMIT = 4000" in javascript
    assert "codePointLength(instruction) > INSTRUCTION_LIMIT" in javascript
    assert "if (immutable)" in submitter
    assert "/engineering-tasks/request" in submitter
    assert "project_version_id: version.id" in submitter
    assert 'agent_provider_id: "codex"' in submitter
    assert "execution_permissions" in submitter
    assert "non_goals: bulletItems(values.nonGoals)" in submitter
    assert "non_goals: []" not in submitter
    assert "install_dependencies: false" in submitter
    assert "external_network: false" in submitter
    assert "body.prohibited_paths = canonicalPathItems(values.prohibitedPaths)" in submitter
    assert "if (finalGitPathPolicyEnabled())" in submitter
    assert "body = { instruction, base_branch: baseBranch, validation_target: validationTarget };" in submitter
    assert "/coding-task-request" in submitter
    assert "Exact rollback contract" in submitter


def test_project_version_copy_distinguishes_immutable_and_legacy_modes():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    assert "ProjectVersion 參考" in index
    assert "只供核准者參考，不會寫入 request" in index
    assert "尚未綁定 immutable revision" in index
    assert "執行時解析" in index
    assert "/versions`" in javascript
    assert "（只供參考，未綁定）" in javascript
    assert "ProjectVersion（必填）" in javascript
    assert "exact commit 已固定" in javascript
    assert "/engineering-tasks/capabilities" in javascript
    assert "state.backendCapabilitiesLoaded" in javascript


def test_runner_gating_requires_all_availability_signals_but_busy_is_allowed():
    javascript = _read(UI_JS)
    availability = _between(
        javascript,
        "function runnerAvailability()",
        "function renderRunnerState(",
    )

    for condition in (
        "!state.runnerConnected",
        "!status.configured",
        "!status.online",
        "!status.codex_installed",
        "!status.authenticated",
    ):
        assert condition in availability
    assert "if (status.busy)" in availability
    busy_branch = availability[availability.index("if (status.busy)") :]
    assert "ready: true" in busy_branch
    assert "核准後可能排隊" in busy_branch


def test_permission_rows_separate_enforced_advisory_and_future_behavior():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    assert 'id="engineering-modify-files" type="checkbox" checked disabled' in index
    assert 'id="engineering-dependencies" type="checkbox" disabled' in index
    assert 'id="engineering-network" type="checkbox" disabled' in index
    assert "技術強制" in index
    assert "代理要求" in index
    assert "後續版本" in index
    assert "不會自動建立或執行 worker Job" in index
    assert "Dependency installation is not authorized by this form" in javascript
    assert "External network access is not authorized by this form" in javascript


def test_v2_final_git_path_policy_is_machine_readable_and_legacy_is_advisory():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    dialog = _between(index, 'id="engineering-task-dialog"', '<div id="project-modal"')
    mode = _between(
        javascript,
        "function engineeringTaskContractVersion()",
        "function runnerAvailability()",
    )
    validation = _between(
        javascript,
        "function validateStep(step",
        "function updatePreview()",
    )
    submitter = _between(
        javascript,
        "async function submitEngineeringTask(event)",
        "function focusableElements(container)",
    )

    assert 'id="engineering-prohibited-paths"' in dialog
    assert 'id="engineering-prohibited"' in dialog
    assert "Prohibited changes（自然語言）" in dialog
    assert "machine-readable path policy" in dialog
    assert "prohibited path 優先於 allowed path" in dialog
    assert "<code>.</code> 表示整個 repository" in dialog
    assert "結尾 <code>/</code> 表示 subtree" in dialog
    assert "只比對 exact path" in dialog
    assert 'id="engineering-allowed-scope-error" class="field-error" hidden' in dialog

    assert 'return engineeringTaskContractVersion() === "engineering-task-v2"' in mode
    assert 'allowedPaths.required = pathPolicyEnabled' in mode
    assert (
        'allowedPaths.setAttribute("aria-required", pathPolicyEnabled ? "true" : "false")'
        in mode
    )
    assert "Runner pre-bundle 與 Server A pre-accept" in mode
    assert "不提供 turn-time filesystem confinement" in mode
    assert "Legacy Coding Task 只把 allowed/prohibited paths 當成 advisory" in mode

    assert "step === 3" in validation
    assert "finalGitPathPolicyEnabled()" in validation
    assert '!element("engineering-allowed-scope").value.trim()' in validation
    assert "showStep(3" in validation
    assert "Boolean(values.allowedPaths)" in javascript

    assert "allowed_paths: finalGitPathPolicyEnabled()" in submitter
    assert "? canonicalPathItems(values.allowedPaths)" in submitter
    assert "body.prohibited_paths = canonicalPathItems(values.prohibitedPaths)" in submitter
    legacy = submitter.split("} else {", 1)[1]
    assert "body = { instruction, base_branch: baseBranch, validation_target: validationTarget };" in legacy
    assert "prohibited_paths" not in legacy


def test_path_policy_preview_describes_final_diff_checks_without_turn_confinement():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    renderer = _between(
        javascript,
        "function renderStructuredInstruction(",
        "function readFormValues()",
    )

    assert 'id="engineering-preview-path-policy"' in index
    assert 'id="engineering-path-policy-permission-copy"' in index
    assert 'id="engineering-path-policy-summary-copy"' in index
    assert "final Git diff" in renderer
    assert "on the Runner before bundling" in renderer
    assert "on Server A before acceptance" in renderer
    assert "does not claim turn-time filesystem confinement" in renderer
    assert "advisory approval requirements in this compatibility mode" in renderer
    assert '"Prohibited paths",' in renderer
    assert 'addSection(parts, "Prohibited changes"' in renderer


def test_role_badge_is_presentation_only_and_uses_safe_auth_me_fields():
    javascript = _read(UI_JS)
    renderer = _between(javascript, "function updateIdentity(authInfo)", "function initialize()")

    assert "actor.platform_admin" in renderer
    assert "authInfo.project_memberships" in renderer
    assert "actor.type" in renderer
    assert "badge.textContent" in renderer
    for forbidden in (
        "actor.email",
        "actor.issuer",
        "actor.subject",
        "session_id",
        "session_token",
    ):
        assert forbidden not in renderer
    assert ".style.display" not in renderer


def test_task_detail_flag_defaults_to_v2_but_accepts_an_explicit_override():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    assert "window.DISPATCH_UI_FLAG_OVERRIDES" in index
    assert '"AI_ENGINEERING_TASK_DETAIL_UI_V1"' in index
    assert ": engineeringTaskUiV2" in index
    assert "function engineeringTaskDetailEnabled()" in javascript
    assert "flags.AI_ENGINEERING_TASK_DETAIL_UI_V1" in javascript


def test_task_list_uses_engineering_endpoint_and_string_id_event_delegation():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    renderer = _between(
        javascript,
        "function renderEngineeringTasks(tasks)",
        "function isConnectionError(error)",
    )

    assert 'api("/engineering-tasks")' in javascript
    assert "window.DispatchUI.loadEngineeringTasks()" in index
    assert 'openButton.dataset.taskId = String(task.id || "")' in renderer
    assert 'openButton.dataset.engineeringTaskOpen = "true"' in renderer
    assert "tbody.addEventListener" not in renderer
    assert "innerHTML" not in renderer
    assert "onclick=" not in renderer
    assert 'element("coding-runs-tbody").addEventListener("click", handleEngineeringTaskListClick)' in javascript
    assert "function clearEngineeringTasks()" in javascript
    assert "++state.listLoadSerial" in javascript
    assert "window.DispatchUI.clearEngineeringTasks()" in index

    # Rollback keeps the original API, renderer, numeric detail bridge, and panel.
    assert 'codingRunsCache = await api("/coding-runs")' in index
    assert "function renderCodingRuns(runs)" in index
    assert "window.openCodingRunDetail" in index
    assert 'id="coding-run-panel"' in index


def test_task_list_failure_states_offer_retry_and_disable_it_while_loading():
    javascript = _read(UI_JS)
    retry_control = _between(
        javascript,
        "function makeRetryControl(kind, label, retry)",
        "function appendLabeledCell(",
    )
    state_renderer = _between(
        javascript,
        "function setEngineeringListState(",
        "function engineeringListHeader()",
    )
    loader = _between(
        javascript,
        "async function loadEngineeringTasks()",
        "function clearEngineeringTasks()",
    )

    assert 'const loading = kind === "loading"' in retry_control
    assert 'loading ? "載入中…" : label' in retry_control
    assert "button.disabled = loading" in retry_control
    assert '!loading && typeof retry === "function"' in retry_control
    assert 'button.addEventListener("click", retry)' in retry_control

    assert '["loading", "error", "disconnected"].includes(kind)' in state_renderer
    assert 'makeRetryControl(kind, "重試載入任務", loadEngineeringTasks)' in state_renderer
    assert 'setEngineeringListState("loading"' in loader
    assert 'disconnected ? "disconnected" : "error"' in loader
    assert 'const serial = ++state.listLoadSerial' in loader
    assert loader.count("serial !== state.listLoadSerial") >= 2
    assert 'api("/engineering-tasks")' in loader


def test_task_detail_retry_preserves_dialog_opener_focus_and_serial_guard():
    javascript = _read(UI_JS)
    state_renderer = _between(
        javascript,
        "function setTaskDetailState(",
        "function detailHeading(",
    )
    opener = _between(
        javascript,
        "async function openEngineeringTaskDetail(",
        "function retryEngineeringTaskDetail()",
    )
    retry = _between(
        javascript,
        "function retryEngineeringTaskDetail()",
        "function closeEngineeringTaskDetail(",
    )

    assert '["loading", "error", "disconnected"].includes(kind)' in state_renderer
    assert (
        'makeRetryControl(kind, "重試載入詳情", retryEngineeringTaskDetail)'
        in state_renderer
    )
    assert "{ preserveOpener = false } = {}" in opener
    assert "if (!preserveOpener) state.detailOpener = document.activeElement" in opener
    assert "const serial = ++state.detailOpenSerial" in opener
    assert opener.count("serial !== state.detailOpenSerial") >= 2
    assert 'api(`/engineering-tasks/${encodeURIComponent(state.detailTaskId)}`)' in opener
    assert 'setTaskDetailState("loading"' in opener
    assert 'disconnected ? "disconnected" : "error"' in opener

    assert "dialog.hidden" in retry
    assert 'element("engineering-task-detail-title").focus({ preventScroll: true })' in retry
    assert "refreshEngineeringTaskDetail()" in retry
    assert "state.detailOpener" not in retry


def test_task_diff_failure_states_retry_only_the_safe_redacted_endpoint():
    javascript = _read(UI_JS)
    state_renderer = _between(
        javascript,
        "function setTaskDiffState(",
        "function normalizeTests(",
    )
    loader = _between(
        javascript,
        "async function loadTaskDiff(",
        "function activateTaskPane(",
    )

    assert 'makeRetryControl(\n      kind,\n      "重試載入 Diff"' in state_renderer
    assert "() => loadTaskDiff({ focusState: true })" in state_renderer
    assert "focusRetry && !retry.disabled" in state_renderer
    assert "retry.focus({ preventScroll: true })" in state_renderer

    assert "{ focusState = false } = {}" in loader
    assert 'setTaskDiffState("loading"' in loader
    assert "target.focus({ preventScroll: true })" in loader
    assert "const serial = state.detailOpenSerial" in loader
    assert loader.count("serial !== state.detailOpenSerial") >= 2
    assert "changes.diff_url === safeDefault" in loader
    assert "const response = await api(endpoint)" in loader
    assert "const disconnected = isConnectionError(error)" in loader
    assert 'disconnected ? "disconnected" : "error"' in loader
    assert "{ focusRetry: focusState }" in loader
    assert "viewer.focus({ preventScroll: true })" in loader


def test_task_detail_dialog_has_accessible_tabs_focus_trap_and_restore():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    dialog = _between(
        index,
        'id="engineering-task-detail-dialog"',
        '<div id="project-modal"',
    )

    assert 'role="dialog"' in dialog
    assert 'aria-modal="true"' in dialog
    assert 'aria-labelledby="engineering-task-detail-title"' in dialog
    assert 'aria-describedby="engineering-task-detail-description"' in dialog
    assert 'id="engineering-task-detail-title" tabindex="-1"' in dialog
    assert dialog.count('role="tab"') == 8
    assert dialog.count('role="tabpanel"') == 8
    for pane in (
        "overview",
        "timeline",
        "commands",
        "changes",
        "tests",
        "artifacts",
        "risks",
        "approvals",
    ):
        assert f'data-task-pane="{pane}"' in dialog
        assert f'data-task-panel="{pane}"' in dialog

    detail_keydown = _between(
        javascript,
        "function handleTaskDetailKeydown(event)",
        "async function runTaskValidationAction()",
    )
    assert 'event.key === "Escape"' in detail_keydown
    assert 'event.key !== "Tab"' in detail_keydown
    assert "focusableElements(dialog)" in detail_keydown
    assert "state.detailOpener = document.activeElement" in javascript
    assert "opener.focus()" in javascript
    assert 'element("engineering-task-detail-title").focus()' in javascript
    assert "window.DispatchUI.closeEngineeringTaskDetail({ restoreFocus });" in index
    assert 'document.body.classList.remove("dialog-open")' in index


def test_task_detail_dialog_isolates_background_and_restores_exact_prior_state():
    javascript = _read(UI_JS)
    isolator = _between(
        javascript,
        "function isolateTaskDetailBackground()",
        "function restoreTaskDetailBackground()",
    )
    restorer = _between(
        javascript,
        "function restoreTaskDetailBackground()",
        "function handleDialogKeydown(event)",
    )
    opener = _between(
        javascript,
        "async function openEngineeringTaskDetail(",
        "function retryEngineeringTaskDetail()",
    )
    closer = _between(
        javascript,
        "function closeEngineeringTaskDetail(",
        "function handleTaskDetailKeydown(event)",
    )
    wizard_opener = _between(
        javascript,
        "function openEngineeringTaskWizard(projectName)",
        "function closeEngineeringTaskWizard(",
    )

    assert "Array.from(document.body.children)" in isolator
    assert 'element("engineering-task-detail-dialog")' in isolator
    assert 'element("modal-backdrop")' in isolator
    assert "candidate !== dialog" in isolator
    assert "candidate !== backdrop" in isolator
    assert 'candidate.matches("script, style")' in isolator
    assert "inert: candidate.inert" in isolator
    assert 'candidate.hasAttribute("aria-hidden")' in isolator
    assert 'candidate.getAttribute("aria-hidden")' in isolator
    assert "candidate.inert = true" in isolator
    assert 'candidate.setAttribute("aria-hidden", "true")' in isolator

    assert "state.detailBackgroundSnapshot = null" in restorer
    assert "snapshot.element.inert = snapshot.inert" in restorer
    assert "snapshot.hadAriaHidden" in restorer
    assert 'snapshot.element.setAttribute("aria-hidden", snapshot.ariaHidden)' in restorer
    assert 'snapshot.element.removeAttribute("aria-hidden")' in restorer
    assert opener.index("isolateTaskDetailBackground()") < opener.index(
        "dialog.hidden = false"
    )
    assert closer.index("dialog.hidden = true") < closer.index(
        "restoreTaskDetailBackground()"
    )

    # Wizard transitions close detail first; legacy modals keep their shared
    # backdrop open, and the mobile navigation's nested state is never rewritten.
    assert "closeEngineeringTaskDetail({ restoreFocus: false })" in wizard_opener
    assert 'document.querySelector(".modal.open")' in closer
    assert "primary-navigation" not in isolator
    assert "app-frame" not in isolator


def test_task_detail_refresh_is_manual_serialized_and_fail_closed():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    dialog = _between(
        index,
        'id="engineering-task-detail-dialog"',
        '<div id="project-modal"',
    )
    state_renderer = _between(
        javascript,
        "function setTaskDetailState(",
        "function detailHeading(",
    )
    disabler = _between(
        javascript,
        "function disableTaskDetailActions()",
        "function detailHeading(",
    )
    refresher = _between(
        javascript,
        "async function refreshEngineeringTaskDetail()",
        "function closeEngineeringTaskDetail(",
    )
    detail_renderer = _between(
        javascript,
        "function renderEngineeringTaskDetail(task)",
        "async function loadTaskDiff(",
    )

    assert 'id="engineering-task-detail-refresh-btn"' in dialog
    assert 'aria-label="重新整理 AI 工程任務詳情"' in dialog
    assert (
        'element("engineering-task-detail-refresh-btn").addEventListener(\n'
        '      "click", refreshEngineeringTaskDetail'
        in javascript
    )
    assert 'querySelector(\'[data-task-pane][aria-selected="true"]\')' in refresher
    assert 'const activePane = selected ? selected.dataset.taskPane : "overview"' in refresher
    assert "const serial = ++state.detailOpenSerial" in refresher
    assert "state.detailTask = null" in refresher
    assert 'refreshButton.disabled = true' in refresher
    assert 'refreshButton.setAttribute("aria-busy", "true")' in refresher
    assert 'setTaskDetailState(\n      "loading"' in refresher
    assert 'api(`/engineering-tasks/${encodeURIComponent(taskId)}`)' in refresher
    assert refresher.count("serial !== state.detailOpenSerial") >= 2
    assert "state.detailTaskId !== taskId" in refresher
    assert "renderEngineeringTaskDetail(detail, { activePane })" in refresher
    assert 'disconnected ? "disconnected" : "error"' in refresher
    assert 'refreshButton.removeAttribute("aria-busy")' in refresher

    assert "disableTaskDetailActions()" in state_renderer
    for action_id in (
        "engineering-task-validation-action",
        "engineering-task-cleanup-action",
        "engineering-task-download-patch-action",
    ):
        assert action_id in disabler
    assert 'button.disabled = true' in disabler
    assert 'delete button.dataset.codingRunId' in disabler
    assert 'delete button.dataset.downloadUrl' in disabler
    assert 'document.querySelectorAll("[data-future-task-action]")' in disabler
    assert 'const activePane = options.activePane || "overview"' in detail_renderer
    assert 'activateTaskPane(activePane, { focus: false })' in detail_renderer
    assert detail_renderer.index("renderTaskActions(task)") < detail_renderer.index(
        'element("engineering-task-detail-actions").hidden = false'
    )


def test_task_timeline_load_more_uses_safe_forward_cursor_and_deduplication():
    javascript = _read(UI_JS)
    paging = _between(
        javascript,
        "function numericTaskEventId(event)",
        "function renderTaskCommands(task)",
    )
    loader = _between(
        javascript,
        "async function loadMoreTaskEvents()",
        "function renderTaskCommands(task)",
    )

    assert "const TASK_EVENT_PAGE_SIZE = 100" in javascript
    assert "Number.isSafeInteger(value) && value > 0" in paging
    assert "events.length === TASK_EVENT_PAGE_SIZE" in paging
    assert "Math.max(...ids)" in paging
    assert 'container.id = "engineering-task-events-pagination"' in paging
    assert 'container.setAttribute("role", "status")' in paging
    assert 'container.setAttribute("aria-live", "polite")' in paging
    assert 'appendTaskEventPagination(panel, events.length)' in paging
    assert '"目前顯示 " + String(eventCount) + " 筆事件。"' in paging
    assert "目前顯示前 100 筆事件" not in paging
    assert 'button.id = "engineering-task-events-load-more"' in paging
    assert 'button.setAttribute("aria-describedby", message.id)' in paging
    assert 'button.addEventListener("click", loadMoreTaskEvents)' in paging

    assert "state.detailEventsLoading" in loader
    assert "state.detailEventCursor === null" in loader
    assert 'button.disabled = true' in loader
    assert 'button.setAttribute("aria-busy", "true")' in loader
    assert (
        '`/events?after_id=${encodeURIComponent(String(cursor))}'
        '&limit=${TASK_EVENT_PAGE_SIZE}`'
        in loader
    )
    assert "const page = await api(endpoint)" in loader
    assert loader.count("serial !== state.detailOpenSerial") >= 2
    assert loader.count("state.detailTaskId !== taskId") >= 2
    assert loader.count("state.detailTask !== task") >= 2
    assert "const seenIds = new Set(" in loader
    assert "seenIds.has(id)" in loader
    assert "seenIds.add(id)" in loader
    assert "id <= cursor" in loader
    assert "task.events = [...current, ...additions]" in loader
    assert "state.detailEventCursor = nextCursor" in loader
    assert "state.detailEventsCanLoadMore = page.length === TASK_EVENT_PAGE_SIZE" in loader
    assert "if (!page.length)" in loader
    empty_page = loader.split("if (!page.length)", 1)[1].split("if (!additions.length", 1)[0]
    assert "state.detailEventsCanLoadMore = false" in empty_page
    assert "state.detailEventsNotice = null" in empty_page
    assert "事件游標未前進，已停止載入以避免重複迴圈" in loader
    assert 'button.textContent = "重試載入更多事件"' in loader
    assert 'disconnected ? "disconnected" : "error"' in loader
    assert 'currentButton.removeAttribute("aria-busy")' in loader


def test_task_detail_uses_safe_attempt_final_response_and_validation_fields():
    javascript = _read(UI_JS)
    attempts = _between(
        javascript,
        "function renderTaskAttempts(task, panel)",
        "function renderTaskFinalResponse(task, panel)",
    )
    final_response = _between(
        javascript,
        "function renderTaskFinalResponse(task, panel)",
        "function renderTaskOverview(task)",
    )
    tests_renderer = _between(
        javascript,
        "function renderTaskTests(task)",
        "function renderTaskArtifacts(task)",
    )
    checkpoint = _between(
        javascript,
        "function taskLatestCheckpoint(task)",
        "function renderTaskAttempts(task, panel)",
    )

    for field in (
        "attempt.attempt_number",
        "attempt.state",
        "attempt.phase",
        "attempt.health",
        "attempt.result_status",
        "attempt.result_commit",
        "attempt.created_at",
        "attempt.started_at",
        "attempt.finished_at",
    ):
        assert field in attempts
    for forbidden in (
        "attempt.runner_server",
        "attempt.staging_job_id",
        "attempt.coding_job_id",
        "attempt.base_commit",
        "attempt.observed_base_commit",
        "attempt.test_summary",
    ):
        assert forbidden not in attempts

    assert final_response.index("if (response.withheld)") < final_response.index(
        'typeof response.content !== "string"'
    )
    assert 'response.available !== true' in final_response
    assert 'makeElement("pre", "task-diff-viewer task-final-response", response.content)' in final_response
    assert 'component-state state-error' in final_response
    assert "innerHTML" not in final_response

    for field in (
        "validation.id",
        "validation.status",
        "validation.attempt_number",
        "validation.approval_id",
        "validation.connection",
        "validation.created_at",
        "validation.updated_at",
        "result.status",
        "result.exit_code",
        "result.finished_at",
    ):
        assert field in tests_renderer
    for forbidden in (
        "validation.target_server",
        "validation.base_commit",
        "validation.result_commit",
        "validation.bundle_push_job_id",
        "validation.downstream_job_id",
        "job.command",
    ):
        assert forbidden not in tests_renderer
    assert "innerHTML" not in tests_renderer

    assert "task.coding_run.result_commit" in checkpoint
    assert "attempts.slice().reverse().find" in checkpoint
    assert 'taskLatestCheckpoint(task), { code: true }' in javascript


def test_task_detail_never_falls_back_to_raw_executor_or_storage_fields():
    javascript = _read(UI_JS)
    overview = _between(
        javascript,
        "function renderTaskOverview(task)",
        "function numericTaskEventId(event)",
    )
    commands = _between(
        javascript,
        "function renderTaskCommands(task)",
        "function renderTaskChanges(task)",
    )
    tests = _between(
        javascript,
        "function renderTaskTests(task)",
        "function renderTaskArtifacts(task)",
    )
    artifacts = _between(
        javascript,
        "function renderTaskArtifacts(task)",
        "function renderTaskRisks(task)",
    )

    assert "task.runner_server" not in overview
    assert 'presentation.runner_connection || "unknown"' in overview

    for forbidden in (
        "command.location",
        "command.execution_location ||",
        "command.working_directory ||",
        "command.output_excerpt",
        "command.log_excerpt",
    ):
        assert forbidden not in commands
    assert 'command.execution_location_label || "執行位置未公開"' in commands
    assert 'command.working_directory_label || "工作目錄未公開"' in commands
    assert 'typeof command.redacted_output === "string"' in commands

    assert "test.command" not in tests
    assert "test.label || test.name || test.kind" in tests
    for forbidden in (
        "artifact.storage_key",
        "artifact.path",
        "artifact.logical_path",
        "artifact.filename",
    ):
        assert forbidden not in artifacts
    assert "Artifact 儲存位置不公開" in artifacts


def test_diff_and_command_log_content_loaders_fail_closed():
    javascript = _read(UI_JS)
    changes = _between(
        javascript,
        "function renderTaskChanges(task)",
        "function setTaskDiffState(",
    )
    diff_loader = _between(
        javascript,
        "async function loadTaskDiff(",
        "function activateTaskPane(",
    )
    commands = _between(
        javascript,
        "function renderTaskCommands(task)",
        "function renderTaskChanges(task)",
    )

    assert "changes.withheld === true" in changes
    assert "changes.available === true" in changes
    assert "changes.withheld === true || changes.available !== true" in diff_loader
    assert diff_loader.index("response.withheld === true") < diff_loader.index(
        "typeof response.patch"
    )
    assert "response.available !== true" in diff_loader
    assert 'typeof response.patch !== "string"' in diff_loader

    assert "logInfo.withheld === true" in commands
    assert "logInfo.available === true" in commands
    assert commands.index("response.withheld === true") < commands.index(
        "typeof response.content"
    )
    assert "response.available === true" in commands
    assert 'typeof response.content === "string"' in commands


def test_task_risks_tab_deduplicates_only_server_returned_text_warnings():
    javascript = _read(UI_JS)
    renderer = _between(
        javascript,
        "function renderTaskRisks(task)",
        "function renderTaskApprovals(task)",
    )

    assert "Array.isArray(task.warnings)" in renderer
    assert "Array.isArray(presentation.warnings)" in renderer
    assert 'typeof warning === "string"' in renderer
    assert "new Set(" in renderer
    assert "task.final_response.withheld === true" in renderer
    assert "task.changes.withheld === true" in renderer
    assert "這不代表已完成全面安全審查" in renderer
    assert "presentationValue(warning)" not in renderer
    assert "innerHTML" not in renderer


def test_task_detail_renders_payload_as_text_and_loads_only_internal_redacted_diff():
    javascript = _read(UI_JS)
    renderers = _between(
        javascript,
        "function renderTaskOverview(task)",
        "function availableAction(task, names)",
    )
    diff_loader = _between(
        javascript,
        "async function loadTaskDiff(",
        "function activateTaskPane(",
    )

    assert "innerHTML" not in renderers
    assert "makeElement" in renderers
    assert "changes.diff_url === safeDefault" in diff_loader
    assert "encodeURIComponent(taskId)" in diff_loader
    assert 'makeElement("pre", "task-diff-viewer"' in diff_loader
    assert "response.withheld === true" in diff_loader
    assert "response.available !== true" in diff_loader
    assert 'typeof response.patch !== "string"' in diff_loader
    assert "const patch = response.patch" in diff_loader
    for forbidden in (
        'typeof response === "string"',
        "response.diff_patch",
        "response.content",
    ):
        assert forbidden not in diff_loader
    assert "redacted_output" in renderers


def test_task_actions_are_server_capability_driven_and_future_actions_stay_disabled():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    renderer = _between(
        javascript,
        "function renderTaskActions(task)",
        "function renderEngineeringTaskDetail(task)",
    )

    assert "task.available_actions" in javascript
    assert '"request_worker_validation"' in renderer
    assert 'availableAction(task, ["cleanup", "cleanup_worktree"])' in renderer
    assert "validation.enabled === true" in renderer
    assert "cleanup.enabled === true" in renderer
    assert "validationRunId != null" in renderer
    assert "validation.engineering_task_id" in renderer
    assert "validation.request_mode" in renderer
    assert "cleanupRunId != null" in renderer
    assert "button.disabled = true" in renderer
    assert "Raw bundles and all remaining future actions stay disabled" in renderer
    assert 'data-future-task-action="continue" disabled' in index
    assert 'data-future-task-action="cancel" disabled' in index
    assert 'data-future-task-action="discard" disabled' in index
    assert 'data-future-task-action="finalize" disabled' in index
    assert 'data-future-task-action="promote" disabled' in index
    assert 'data-future-task-action="create_draft_pr" disabled' in index


def test_sanitized_collected_patch_download_is_exactly_server_gated():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    authorizer = _between(
        javascript,
        "function authorizedPatchDownload(task)",
        "function renderTaskActions(task)",
    )
    renderer = _between(
        javascript,
        "function renderTaskActions(task)",
        "function renderEngineeringTaskDetail(task)",
    )
    downloader = _between(
        javascript,
        "async function runTaskPatchDownloadAction()",
        "function handleEngineeringTaskListClick(event)",
    )

    assert '下載去敏後的已收集 patch' in index
    assert 'id="engineering-task-download-patch-action"' in index
    assert 'data-future-task-action="download_patch"' in index
    assert 'availableAction(task, ["download_patch"])' in authorizer
    assert "action.enabled === true" in authorizer
    assert "url === expectedUrl" in authorizer
    assert "`/engineering-tasks/${encodeURIComponent(taskId)}/patch`" in authorizer
    assert "patchButton.disabled = !patchDownload.url" in renderer
    assert "button.dataset.downloadUrl !== patchDownload.url" in downloader
    assert "authenticatedDownload(patchDownload.url)" in downloader
    assert "URL.createObjectURL(result.blob)" in downloader
    assert "URL.revokeObjectURL(objectUrl)" in downloader
    assert "result.redacted ? \".redacted\" : \"\"" in downloader
    assert "engineering-task-${patchDownload.taskId}" in downloader
    for forbidden in ("task.title", "task.project", "project_name", "Content-Disposition"):
        assert forbidden not in downloader


def test_raw_bundle_download_remains_permanently_withheld():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    renderer = _between(
        javascript,
        "function renderTaskActions(task)",
        "function renderEngineeringTaskDetail(task)",
    )

    assert 'data-future-task-action="download_bundle"' in index
    assert 'Download bundle（原始 bundle 不提供）' in index
    assert 'button.dataset.futureTaskAction === "download_bundle"' in renderer
    assert "原始 bundle 可能含未去敏內容" in renderer
    assert "runTaskBundle" not in javascript


def test_engineering_owned_jobs_do_not_offer_generic_cancel_diagnose_or_rerun():
    index = _read(INDEX_HTML)
    renderer = _between(index, "function renderJobs(jobs)", "window.cancelJob")

    assert "const engineeringOwned = Boolean(j.engineering_task_id)" in renderer
    assert "const validationLinked = Boolean(j.engineering_validation_request_id)" in renderer
    assert "const engineeringProtected = engineeringOwned || validationLinked" in renderer
    assert 'j.status === "queued" && !engineeringOwned' in renderer
    assert 'j.status === "failed" && !engineeringProtected' in renderer
    assert "if (engineeringProtected)" in renderer
    assert "openEngineeringTaskForJob(${j.id})" in renderer
    assert "window.openEngineeringTaskForJob" in index
    assert "job.engineering_task_id || job.validation_engineering_task_id" in index
    assert "DispatchUI.openEngineeringTaskDetail(taskId)" in index


def test_native_worker_validation_uses_always_pending_endpoint_and_legacy_stays_dispatch():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    submit = _between(
        index,
        'document.getElementById("dispatch-submit-btn").addEventListener',
        "// ---- AI 寫程式任務",
    )
    bridge = _between(
        index,
        "window.openValidationDispatchFromEngineeringTask",
        "window.cleanupCodingRunRequest",
    )
    action = _between(
        javascript,
        "async function runTaskValidationAction()",
        "async function runTaskCleanupAction()",
    )

    assert "dispatchEngineeringTaskId" in submit
    assert "/worker-validation-request" in submit
    assert 'pinServer === "_local"' in submit
    assert 'priority: "normal"' in submit
    assert 'source: "web"' not in submit.split("if (dispatchEngineeringTaskId)", 1)[1].split("return;", 1)[0]
    assert 'requestMode === "legacy_dispatch"' in bridge
    assert "window.openValidationDispatchFromCodingRun(run.id)" in bridge
    assert "dispatchEngineeringTaskId = normalizedTaskId" in bridge
    assert "pending enqueue approval" in bridge
    assert "button.dataset.engineeringTaskId" in action
    assert "button.dataset.requestMode" in action


def test_validation_and_cleanup_actions_ignore_stale_task_detail_after_every_await():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    capture = _between(
        javascript,
        "function captureTaskDetailAction()",
        "function renderTaskActions(task)",
    )
    validation = _between(
        javascript,
        "async function runTaskValidationAction()",
        "async function runTaskCleanupAction()",
    )
    cleanup = _between(
        javascript,
        "async function runTaskCleanupAction()",
        "async function runTaskPatchDownloadAction()",
    )
    bridge = _between(
        index,
        "window.openValidationDispatchFromEngineeringTask",
        "window.cleanupCodingRunRequest",
    )

    for field in (
        "taskId: state.detailTaskId",
        "detailSerial: state.detailOpenSerial",
        "detailTask: state.detailTask",
    ):
        assert field in capture
    assert "snapshot.detailSerial === state.detailOpenSerial" in capture
    assert "snapshot.taskId === state.detailTaskId" in capture
    assert "snapshot.detailTask === state.detailTask" in capture
    assert "!dialog.hidden" in capture

    validation_await = validation.index(
        "await window.openValidationDispatchFromEngineeringTask("
    )
    assert "const snapshot = captureTaskDetailAction()" in validation
    assert "const engineeringTaskId = button.dataset.engineeringTaskId" in validation
    assert "const codingRunId = button.dataset.codingRunId" in validation
    assert "const requestMode = button.dataset.requestMode" in validation
    assert "() => taskDetailActionIsCurrent(snapshot)" in validation
    assert validation.index(
        "if (!taskDetailActionIsCurrent(snapshot)) return;", validation_await
    ) < validation.index("closeEngineeringTaskDetail", validation_await)
    assert validation.count("if (!taskDetailActionIsCurrent(snapshot)) return;") >= 2

    assert "const snapshot = captureTaskDetailAction()" in cleanup
    api_await = cleanup.index("await api(")
    list_await = cleanup.index("await loadEngineeringTasks()")
    refresh_await = cleanup.index("await refreshPromise")
    first_guard = cleanup.index(
        "if (!taskDetailActionIsCurrent(snapshot)) return;", api_await
    )
    second_guard = cleanup.index(
        "if (!taskDetailActionIsCurrent(snapshot)) return;", list_await
    )
    final_guard = cleanup.index("state.detailTaskId !== snapshot.taskId", refresh_await)
    assert api_await < first_guard < list_await < second_guard < refresh_await < final_guard
    assert "openEngineeringTaskDetail" not in cleanup
    assert "state.detailOpener" not in cleanup

    bridge_await = bridge.index("const run = await api(")
    bridge_guard = bridge.index(
        'typeof isCurrent === "function" && !isCurrent()', bridge_await
    )
    assert bridge_guard < bridge.index("codingRunDetailCache = run")
    assert "return false" in bridge


def test_task_statuses_distinguish_failure_interruption_disconnect_and_unknown():
    css = _read(UI_CSS)
    javascript = _read(UI_JS)
    status_contract = _between(
        javascript,
        "const ENGINEERING_TASK_STATUS",
        "const state =",
    )

    expected = {
        "failed": ("失敗", "×"),
        "interrupted": ("已中斷", "◇"),
        "disconnected": ("連線中斷", "↯"),
        "unknown": ("狀態未知", "?"),
    }
    for key, (label, icon) in expected.items():
        assert f"{key}:" in status_contract
        assert label in status_contract
        assert icon in status_contract
        assert f".status-{key}" in css
    assert 'path_policy_violation: { label: "路徑政策拒絕", icon: "⊘" }' in status_contract
    assert 'key === "path_policy_violation" ? "failed" : key' in javascript
    assert '"pending_approval", "path_policy_violation", "secret_violation"' in javascript
    assert "fixedPolicyLabel ? copy.label : (serverLabel || copy.label)" in javascript
    assert "disconnected 不代表任務 failed" in javascript


def test_safety_rejection_and_waiting_approval_are_not_presented_as_failure_or_queue():
    css = _read(UI_CSS)
    javascript = _read(UI_JS)
    status_contract = _between(
        javascript,
        "const ENGINEERING_TASK_STATUS",
        "const state =",
    )
    normalizer = _between(
        javascript,
        "function safeStatusKey(value)",
        "function presentationValue(",
    )
    validations = _between(
        javascript,
        "function renderTaskTests(task)",
        "function renderTaskArtifacts(task)",
    )

    assert 'pending_approval: { label: "等待核准", icon: "⌛" }' in status_contract
    assert 'secret_violation: { label: "安全政策拒絕", icon: "⛨" }' in status_contract
    assert 'pending_approval: "queued"' not in normalizer
    assert 'setStatusBadge(badge, validation.status || "unknown")' in validations
    assert ".status-pending_approval" in css
    assert 'content: "⌛"' in css
    assert ".status-secret_violation" in css
    assert "background: var(--ui-safety-rejected)" in css
    assert 'content: "⛨"' in css
    assert ".status-secret_violation { background: var(--ui-danger); }" not in css


def test_task_detail_and_table_have_mobile_fallback_without_page_overflow():
    css = _read(UI_CSS)

    assert ".task-detail-dialog" in css
    assert ".task-detail-tablist" in css
    assert "overflow-x: auto" in css
    assert '#engineering-task-table.task-centric td::before' in css
    assert 'content: attr(data-label)' in css
    assert ".task-diff-viewer" in css
    assert "white-space: pre" in css
    assert "word-break: normal" in css
    assert "#engineering-task-detail-title" in css
    assert "overflow-wrap: anywhere" in css
    assert ".task-test > code" in css
    assert ".task-artifact-meta" in css
    assert ".task-detail-action-bar" in css
    assert "overflow-x: auto" in _between(
        css, ".task-detail-action-bar {", "}"
    )
    assert "overflow-x: hidden" not in _between(css, "body {", "}")
