"""Static contracts for the bounded supporting-surface redesign.

Plan v2 Slice 8 progressively reorganizes the existing dependency-free
frontend.  It does not add backend capabilities, infer authorization in the
browser, or replace the established hash routes and action/API contracts.
These source-level tests intentionally avoid requiring a browser runtime.
"""

import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "static" / "index.html"
UI_CSS = ROOT / "static" / "ui.css"
UI_JS = ROOT / "static" / "ui.js"

LEGACY_TABS = (
    "overview",
    "projects",
    "datasets",
    "jobs",
    "servers",
    "coding-runs",
    "chat",
)


class _StaticHtmlAuditParser(HTMLParser):
    """Collect source-authored controls and ARIA ID references before scripts."""

    VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.ids: list[str] = []
        self.labels_for: set[str] = set()
        self.controls: list[tuple[int, str, dict[str, str | None], bool]] = []
        self.references: list[tuple[int, str, str, str]] = []
        self.dialogs: list[tuple[int, dict[str, str | None]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        line = self.getpos()[0]
        wrapped_by_label = any(ancestor == "label" for ancestor, _attrs in self.stack)
        element_id = attributes.get("id")
        if element_id:
            self.ids.append(element_id)
        if tag == "label" and attributes.get("for"):
            self.labels_for.add(str(attributes["for"]))
        if tag in {"input", "select", "textarea"}:
            self.controls.append((line, tag, attributes, wrapped_by_label))
        for attribute in ("aria-labelledby", "aria-describedby", "aria-controls"):
            for target in str(attributes.get(attribute) or "").split():
                self.references.append((line, element_id or tag, attribute, target))
        if attributes.get("role") == "dialog":
            self.dialogs.append((line, attributes))
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, attributes))

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                self.stack = self.stack[:index]
                return


def _parse_static_html() -> _StaticHtmlAuditParser:
    parser = _StaticHtmlAuditParser()
    parser.feed(_read(INDEX_HTML))
    return parser


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def _panel(panel_id: str, next_panel_id: str) -> str:
    return _between(
        _read(INDEX_HTML),
        f'<section id="{panel_id}"',
        f'<section id="{next_panel_id}"',
    )


def _section(markup: str, section_id: str, next_section_id: str | None = None) -> str:
    start = markup.index(f'id="{section_id}"')
    if next_section_id is None:
        return markup[start:]
    end = markup.index(f'id="{next_section_id}"', start)
    return markup[start:end]


def _javascript_function(source: str, name: str) -> str:
    """Return one named JavaScript function, ignoring braces in strings/comments."""

    match = re.search(rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", source)
    assert match is not None, f"missing JavaScript function {name}"
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


def _opening_tag(source: str, tag: str, element_id: str) -> str:
    match = re.search(
        rf'<{re.escape(tag)}\b[^>]*\bid="{re.escape(element_id)}"[^>]*>', source
    )
    assert match is not None, f"missing <{tag}> with id={element_id}"
    return match.group(0)


def _assert_semantic_navigation(markup: str, controlled_ids: tuple[str, ...]) -> None:
    navigations = re.findall(r"<nav\b[^>]*>.*?</nav>", markup, re.DOTALL)
    matching = [
        navigation
        for navigation in navigations
        if all(f'aria-controls="{section_id}"' in navigation for section_id in controlled_ids)
    ]
    assert matching, f"missing semantic navigation for {controlled_ids}"
    assert any("aria-label=" in navigation for navigation in matching)


def _assert_named_sections(markup: str, section_ids: tuple[str, ...]) -> None:
    for section_id in section_ids:
        opening = re.search(
            rf'<section\b[^>]*\bid="{re.escape(section_id)}"[^>]*>', markup
        )
        assert opening is not None, f"missing semantic section {section_id}"
        assert "aria-labelledby=" in opening.group(0), f"{section_id} needs an accessible name"


def test_existing_hash_routes_panels_and_dependency_free_assets_are_preserved():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)

    for tab in LEGACY_TABS:
        assert f'data-tab="{tab}"' in index
        assert f'id="tab-{tab}"' in index
        assert f'"{tab}"' in _between(index, "const KNOWN_TABS", ");")

    manifests = (
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "vite.config.js",
        "webpack.config.js",
    )
    assert all(not (ROOT / manifest).exists() for manifest in manifests)
    assert len(re.findall(r'<script\b[^>]*\bsrc="[^"]+"', index)) == 1
    assert len(
        re.findall(r'<link\b[^>]*\brel="stylesheet"[^>]*\bhref="[^"]+"', index)
    ) == 1
    assert "https://" not in index
    for forbidden in ("import ", "require(", "node_modules", "React.", "Vue."):
        assert forbidden not in javascript


def test_nine_destination_primary_ia_uses_additive_aliases_and_one_work_surface():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    primary = _between(index, '<nav class="tabs" aria-label="平台區域">', "</nav>")
    expected = (
        ("overview", "總覽"),
        ("projects", "專案"),
        ("work", "工作"),
        ("approvals", "核准"),
        ("datasets", "資料與結果"),
        ("servers", "基礎設施"),
        ("activity", "活動與稽核"),
        ("administration", "管理"),
        ("chat", "助手"),
    )
    assert primary.count("<button ") == len(expected)
    for route, label in expected:
        assert f'data-tab="{route}"' in primary
        assert label in primary

    route_targets = _between(index, "const TAB_ROUTE_TARGETS", "function primaryTabForRoute")
    assert 'work: Object.freeze({ panel: "jobs" })' in route_targets
    assert 'panel: "overview"' in route_targets
    for section in (
        "overview-approvals",
        "overview-activity-audit",
        "overview-administration",
    ):
        assert section in route_targets
    assert 'validation: Object.freeze({ panel: "jobs" })' in route_targets

    work_navigations = re.findall(
        r'<nav class="work-subnav"[^>]*>.*?</nav>', index, re.DOTALL
    )
    assert len(work_navigations) == 2
    for navigation in work_navigations:
        for route in ("jobs", "coding-runs", "validation"):
            assert f'data-work-route="{route}"' in navigation
            assert f'href="#tab/{route}"' in navigation
    assert index.count('id="tab-jobs"') == 1
    assert index.count('id="tab-coding-runs"') == 1
    assert 'id="tab-validation"' not in index
    for route in ("work", "approvals", "activity", "administration", "validation"):
        assert f'{route}: "' in javascript or f'"{route}": "' in javascript


def test_overview_has_approval_activity_and_administration_landmarks():
    overview = _panel("tab-overview", "tab-projects")
    sections = (
        "overview-approvals",
        "overview-activity-audit",
        "overview-administration",
    )

    _assert_semantic_navigation(overview, sections)
    _assert_named_sections(overview, sections)
    assert overview.index('id="overview-approvals"') < overview.index(
        'id="overview-activity-audit"'
    )
    assert overview.index('id="overview-activity-audit"') < overview.index(
        'id="overview-administration"'
    )

    # Existing dynamic targets remain present, merely moved into clearer areas.
    approvals = _section(overview, "overview-approvals", "overview-activity-audit")
    activity = _section(overview, "overview-activity-audit", "overview-administration")
    assert 'id="approval-cards"' in approvals
    assert 'id="events-list"' in activity


def test_approvals_have_semantic_views_and_server_evidenced_categories():
    overview = _panel("tab-overview", "tab-projects")
    approvals = _section(overview, "overview-approvals", "overview-activity-audit")
    views = ("approvals-pending", "approvals-requested", "approvals-history")

    _assert_semantic_navigation(approvals, views)
    _assert_named_sections(approvals, views)
    renderer = _javascript_function(_read(INDEX_HTML), "renderSupportingApprovals")
    evidence = f"{approvals}\n{renderer}"
    for label in ("AI 工程", "Runtime Job", "基礎設施", "部署"):
        assert label in evidence

    pending = _section(approvals, "approvals-pending", "approvals-requested")
    assert "待我核准" not in pending
    assert "待核准" in pending
    assert "全平台" in pending or "可見" in pending

    requested = _section(approvals, "approvals-requested", "approvals-history")
    requested_evidence = f"{requested}\n{renderer}"
    assert "requester_actor_id" in renderer
    assert "actor.id" in renderer
    assert "!= null" in renderer or "!== null" in renderer
    assert "legacy" in requested_evidence.lower() or "未知" in requested_evidence

    direct_identity_match = re.search(
        r"requester_actor_id\s*===\s*[^;\n]{0,120}actor\.id"
        r"|actor\.id\s*===\s*[^;\n]{0,120}requester_actor_id",
        renderer,
    )
    actor_id_alias = re.search(
        r"(?:const|let)\s+([A-Za-z_$][\w$]*)\s*=\s*[^;\n]*actor\.id",
        renderer,
    )
    alias_identity_match = None
    if actor_id_alias is not None:
        alias = re.escape(actor_id_alias.group(1))
        alias_identity_match = re.search(
            rf"requester_actor_id\s*===\s*{alias}\b"
            rf"|\b{alias}\s*===\s*[^;\n]{{0,120}}requester_actor_id",
            renderer,
        )
    assert direct_identity_match is not None or alias_identity_match is not None

    assert "approval.kind" in renderer or ".kind" in renderer
    assert "approval.status" in renderer or ".status" in renderer
    # Ownership is server-evidenced only when the persisted requester actor ID
    # is non-null and strictly equals /auth/me.actor.id.  Legacy null rows must
    # remain unknown instead of being treated as the ambient person.
    assert not re.search(r"(?<![=!])==(?!=)", renderer)
    for forbidden in (
        "currentUser",
        "currentActor",
        "actor.email",
        "actor.subject",
        "requester_display",
        "requester_name",
    ):
        assert forbidden not in renderer


def test_infrastructure_separates_runner_workers_and_inventory_without_losing_actions():
    infrastructure = _panel("tab-servers", "tab-coding-runs")
    sections = (
        "infrastructure-runner",
        "infrastructure-workers",
        "infrastructure-inventory",
    )

    _assert_semantic_navigation(infrastructure, sections)
    _assert_named_sections(infrastructure, sections)
    runner = _section(infrastructure, "infrastructure-runner", "infrastructure-workers")
    workers = _section(infrastructure, "infrastructure-workers", "infrastructure-inventory")
    inventory = _section(infrastructure, "infrastructure-inventory")

    assert "Coding Runner" in runner
    assert 'id="server-config-tbody"' in workers
    assert 'id="new-server-btn"' in workers
    assert 'id="candidate-list"' in inventory
    for action_id in (
        "candidate-server-select",
        "candidate-status-select",
        "candidate-refresh-btn",
        "candidate-scan-btn",
        "candidate-ignore-nested-btn",
        "manual-candidate-submit-btn",
    ):
        assert f'id="{action_id}"' in inventory

    # The Runner is a distinct execution role, not relabelled as a GPU worker.
    assert 'id="server-config-tbody"' not in runner
    assert 'id="candidate-list"' not in workers


def test_data_surface_separates_registered_inputs_from_honest_result_boundaries():
    data_panel = _panel("tab-datasets", "tab-jobs")
    sections = ("data-datasets", "data-results-artifacts")

    _assert_semantic_navigation(data_panel, sections)
    _assert_named_sections(data_panel, sections)
    datasets = _section(data_panel, "data-datasets", "data-results-artifacts")
    results = _section(data_panel, "data-results-artifacts")

    assert 'id="new-dataset-btn"' in datasets
    assert 'id="dataset-list"' in datasets
    for term in ("Results", "Artifacts"):
        assert term in results
    assert "尚未" in results
    assert "lineage" in results.lower() or "關聯" in results
    # Do not present a fake mutation until a result/artifact backend exists.
    assert not re.search(r"<(?:button|input|select|textarea)\b", results)


def test_runtime_jobs_distinguish_custom_commands_run_profiles_and_job_history():
    runtime = _panel("tab-jobs", "tab-servers")
    sections = ("runtime-custom-command", "runtime-run-profiles", "runtime-job-list")

    _assert_semantic_navigation(runtime, sections)
    _assert_named_sections(runtime, sections)
    custom = _section(runtime, "runtime-custom-command", "runtime-run-profiles")
    profiles = _section(runtime, "runtime-run-profiles", "runtime-job-list")
    job_list = _section(runtime, "runtime-job-list")

    assert "custom command" in custom.lower()
    assert "enqueue" in custom.lower() or "核准" in custom
    assert "Run Profiles" in profiles
    # D5 Run Profile v1 之後這個 surface 描述的是「已持久化、approval-gated、
    # feature flag 守門」的真實能力，不再是 future placeholder；但管理表單
    # 在專案詳情頁，這裡仍然不得出現任何輸入控制項。
    assert "RUN_PROFILE_V1_ENABLED" in profiles
    assert "approval" in profiles
    assert not re.search(r"<(?:input|select|textarea)\b", profiles)
    assert 'id="jobs-tbody"' in job_list
    assert "<table" in job_list


def test_activity_audit_and_administration_expose_only_honest_available_evidence():
    overview = _panel("tab-overview", "tab-projects")
    activity = _section(overview, "overview-activity-audit", "overview-administration")
    administration = _section(overview, "overview-administration")

    assert "Activity" in activity or "活動" in activity
    assert "Audit" in activity or "稽核" in activity
    assert "/events" in activity
    assert "/audit" in activity
    assert "Administration" in administration or "管理" in administration
    assert "尚未" in administration or "feature flag" in administration
    assert "authorization" in administration.lower() or "授權" in administration

    renderer = _javascript_function(_read(INDEX_HTML), "renderAdministrationSummary")
    for allowed in (
        "actor.type",
        "actor.platform_admin",
        "project_memberships",
    ):
        assert allowed in renderer
    for sensitive in (
        "actor.email",
        "actor.subject",
        "actor.issuer",
        "session_id",
        "session_token",
        "cookie",
    ):
        assert sensitive not in renderer
    for authorization_control in (
        ".disabled",
        ".remove()",
        'style.display = "none"',
        ".hidden = true",
    ):
        assert authorization_control not in renderer


def test_supporting_surfaces_use_accessible_distinct_async_states_with_retry():
    index = _read(INDEX_HTML)
    css = _read(UI_CSS)
    renderer = _javascript_function(index, "supportingSurfaceStateMarkup")

    assert 'role="status"' in renderer
    assert 'aria-live="polite"' in renderer
    assert 'aria-atomic="true"' in renderer
    for state, icon in (
        ("loading", "◌"),
        ("empty", "∅"),
        ("error", "×"),
        ("disconnected", "↯"),
    ):
        assert state in renderer
        assert icon in renderer
        assert f".component-state.state-{state}" in css
    assert "retry" in renderer.lower()
    assert "button" in renderer.lower()
    assert "disconnected" in renderer


def test_slice_8_reuses_existing_read_apis_and_material_action_ids():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    refresh = _javascript_function(index, "refreshAll")
    runner_loader = _javascript_function(
        index, "loadCodingRunnerInfrastructureStatus"
    )
    tab_activator = _javascript_function(index, "activateTab")
    section_activator = _javascript_function(
        index, "activateSupportingSurfaceSection"
    )

    for endpoint in (
        'api("/auth/me")',
        'api("/servers")',
        'api("/jobs")',
        'api("/approvals")',
        'api("/events")',
        'api("/projects")',
        'api("/datasets")',
        'api("/server-config")',
    ):
        assert endpoint in refresh
    # Runner status can perform a read-only SSH probe when its backend cache is
    # stale. Merely entering the top-level Infrastructure tab must keep the
    # default Worker section probe-free. The probe is limited to selecting the
    # Runner subsection, explicit refresh/retry, or opening a coding-task form.
    assert 'api("/codex-runner/status")' not in refresh
    assert 'api("/codex-runner/status")' in runner_loader
    assert 'panelTab === "servers"' in tab_activator
    assert "loadCodingRunnerInfrastructureStatus()" not in tab_activator

    runner_call = section_activator.index(
        "loadCodingRunnerInfrastructureStatus()"
    )
    assert section_activator.index('group === "infrastructure"') < runner_call
    assert (
        section_activator.index('sectionId === "infrastructure-runner"')
        < runner_call
    )
    assert section_activator.index("applicationRequestsEnabled") < runner_call

    servers_panel = _panel("tab-servers", "tab-coding-runs")
    runner_control = _opening_tag(
        servers_panel, "button", "coding-runner-refresh-btn"
    )
    runner_section = _opening_tag(
        servers_panel, "section", "infrastructure-runner"
    )
    worker_section = _opening_tag(
        servers_panel, "section", "infrastructure-workers"
    )
    worker_control = re.search(
        r'<button\b[^>]*data-supporting-section="infrastructure-workers"[^>]*>',
        servers_panel,
    )
    assert worker_control is not None
    assert 'hidden' in runner_section
    assert 'hidden' not in worker_section
    assert 'aria-current="page"' in worker_control.group(0)
    assert 'type="button"' in runner_control

    assert 'id="coding-runner-refresh-btn"' in index
    retry_wiring_start = index.index(
        'document.getElementById("coding-runner-refresh-btn")'
    )
    retry_wiring = index[retry_wiring_start : retry_wiring_start + 650]
    assert 'addEventListener("click"' in retry_wiring
    assert "loadCodingRunnerInfrastructureStatus" in retry_wiring
    assert 'data-supporting-retry="runner"' in retry_wiring

    legacy_opener = _between(
        index,
        "window.openCodingTaskModal = function",
        'document.getElementById("coding-task-cancel-btn")',
    )
    wizard_runner_loader = _javascript_function(javascript, "refreshRunnerStatus")
    wizard_opener = _javascript_function(javascript, "openEngineeringTaskWizard")
    assert "loadCodingRunnerInfrastructureStatus()" in legacy_opener
    assert 'api("/codex-runner/status")' in wizard_runner_loader
    assert "refreshRunnerStatus(openSerial)" in wizard_opener

    # Four legacy-page call sites are intentional: Runner subsection, legacy
    # modal, refresh, and retry. This makes an accidental top-level/tab poll a
    # visible contract change. The v2 wizard owns its separate loader/retry.
    assert index.count("loadCodingRunnerInfrastructureStatus();") == 4
    assert javascript.count("refreshRunnerStatus(") == 3
    for invented_endpoint in (
        "/approvals/requested",
        "/approvals/history",
        "/administration",
        "/results",
        "/artifacts",
        "/run-profiles",
    ):
        assert invented_endpoint not in refresh

    # Reorganization must preserve the controls already wired to current
    # approval-gated mutations and list/detail renderers.
    for element_id in (
        "approval-cards",
        "approval-panel-cards",
        "approval-fab",
        "events-list",
        "new-dataset-btn",
        "dataset-list",
        "jobs-tbody",
        "new-server-btn",
        "server-config-tbody",
        "candidate-list",
        "candidate-scan-btn",
        "candidate-ignore-nested-btn",
        "manual-candidate-submit-btn",
    ):
        assert f'id="{element_id}"' in index


def test_periodic_refresh_is_partial_failure_safe_and_auth_epoch_guarded():
    index = _read(INDEX_HTML)
    refresh = _javascript_function(index, "refreshAll")
    settler = _javascript_function(index, "settleRefreshRequest")
    unavailable = _javascript_function(index, "refreshUnavailableMarkup")
    unauthorized = _javascript_function(index, "handleUnauthorizedResponse")
    stop_protected = _javascript_function(index, "stopProtectedActivity")

    resources = (
        ("serversResult", "/servers", "Worker health", "renderServers"),
        ("jobsResult", "/jobs", "Runtime Jobs", "renderJobs"),
        ("approvalsResult", "/approvals", "Approvals", "renderApprovals"),
        ("eventsResult", "/events", "Activity / audit", "renderEvents"),
        ("projectsResult", "/projects", "Projects", "renderProjects"),
        ("datasetsResult", "/datasets", "Datasets", "renderDatasets"),
        (
            "serverConfigsResult",
            "/server-config",
            "Worker configuration",
            "renderServerConfigTable",
        ),
    )
    assert refresh.count("settleRefreshRequest(api(") == len(resources)
    for result, endpoint, label, renderer in resources:
        assert f'settleRefreshRequest(api("{endpoint}"), "{label}")' in refresh
        assert f"if ({result}.ok)" in refresh
        assert f"{renderer}({result}.value" in refresh
        assert f'refreshUnavailableMarkup("{label}")' in refresh

    # Every resource owns an independent top-level success/error branch. One
    # rejected request is converted to an ok:false value and cannot reject the
    # aggregate or suppress successful sibling renderers.
    independent_results = re.findall(
        r"(?m)^    if \((\w+Result)\.ok\) \{", refresh
    )
    assert set(independent_results) == {resource[0] for resource in resources}
    assert "return promise.then(" in settler
    assert "{ ok: true, value, resource }" in settler
    assert "{ ok: false, value: null, resource }" in settler
    assert 'supportingSurfaceStateMarkup(\n    "error"' in unavailable
    assert '"refresh-all"' in unavailable
    retry_handler = _between(
        index,
        'document.addEventListener("click", (event) => {\n  if (event.target.closest(\'[data-supporting-retry="refresh-all"]\'))',
        "async function refreshAll()",
    )
    assert "refreshAll()" in retry_handler

    # A 401 increments the authentication epoch and disables protected
    # requests. The post-await guard precedes every result render, so late
    # sibling responses cannot repopulate UI cleared for the expired actor.
    assert "if (!applicationRequestsEnabled) return;" in refresh
    snapshot_at = refresh.index(
        "const refreshAuthSerial = authInitializationSerial"
    )
    aggregate_at = refresh.index("await Promise.all(")
    guard_at = refresh.index(
        "if (!applicationRequestsEnabled || "
        "refreshAuthSerial !== authInitializationSerial) return;"
    )
    first_result_at = min(
        refresh.index(f"if ({result}.ok)") for result, *_rest in resources
    )
    assert snapshot_at < aggregate_at < guard_at < first_result_at
    assert "authInitializationSerial += 1" in unauthorized
    assert "stopProtectedActivity()" in unauthorized
    assert "clearProtectedUi()" in unauthorized
    assert "applicationRequestsEnabled = false" in stop_protected


def test_supporting_navigation_is_presentation_only_not_role_authorization():
    index = _read(INDEX_HTML)
    javascript = _read(UI_JS)
    combined = f"{index}\n{javascript}"

    # Every section is present in static markup for every authenticated actor.
    for section_id in (
        "overview-approvals",
        "overview-activity-audit",
        "overview-administration",
        "infrastructure-runner",
        "infrastructure-workers",
        "infrastructure-inventory",
        "data-datasets",
        "data-results-artifacts",
        "runtime-custom-command",
        "runtime-run-profiles",
        "runtime-job-list",
    ):
        assert combined.count(f'id="{section_id}"') == 1

    navigator = _javascript_function(index, "activateSupportingSurfaceSection")
    assert "aria-current" in navigator
    assert ".hidden" in navigator
    for role_signal in (
        "platform_admin",
        "project_memberships",
        "actor.type",
        "service",
    ):
        assert role_signal not in navigator
    for material_action in (
        "approve",
        "reject",
        "candidate-scan-btn",
        "new-server-btn",
        "new-dataset-btn",
    ):
        assert material_action not in navigator
    for forbidden in (".disabled", ".remove()", "fetch(", "api("):
        assert forbidden not in navigator


def test_coding_runner_surface_distinguishes_every_availability_signal():
    index = _read(INDEX_HTML)
    renderer = _javascript_function(index, "renderCodingRunnerInfrastructureStatus")

    expected_conditions = (
        "!codexRunnerStatusConnected",
        "!status.configured",
        "status.online !== true",
        'status.probe_status === "probe_failed"',
        "status.codex_installed === false",
        "status.authenticated !== true",
        "status.busy",
    )
    positions = [renderer.index(condition) for condition in expected_conditions]
    assert positions == sorted(positions)

    for distinct_copy in (
        "status endpoint 無法連線",
        "尚未設定",
        "Runner 離線",
        "能力探測失敗",
        "Codex 尚未安裝",
        "Codex 尚未登入",
        "仍可送出 approval",
        "busy 不等於 unavailable",
    ):
        assert distinct_copy in renderer

    # Busy is queueing information, not an unavailable or authorization state.
    busy_branch = renderer[renderer.index("status.busy") :]
    assert "state-success" in busy_branch
    assert "approval" in busy_branch
    for forbidden in (".disabled", ".hidden", "setAttribute(\"disabled\"", "remove()"):
        assert forbidden not in busy_branch


def test_coding_task_approval_separates_pinned_engineering_and_legacy_contracts():
    index = _read(INDEX_HTML)
    renderer = _javascript_function(index, "codingTaskBodyHtml")
    immutable_start = renderer.index("if (immutable)")
    legacy_start = renderer.index("base branch：", immutable_start)
    engineering = renderer[immutable_start:legacy_start]
    legacy = renderer[legacy_start:]

    assert "const immutable" in renderer
    for discriminator in (
        "p.engineering_task_id",
        "p.contract_version",
        "p.project_version_id",
        "p.base_commit",
    ):
        assert discriminator in renderer[:immutable_start]

    for field in (
        "contract_version",
        "project_version_id",
        "base_commit",
        "agent_provider_id",
        "execution_contract",
    ):
        assert f"p.{field}" in engineering
    assert "escapeHtml" in engineering
    assert "immutable" in engineering.lower()
    for contract_field in (
        "contract.runner",
        "contract.source_kind",
        "contract.workspace_rel",
        "dependency_installation",
    ):
        assert contract_field in engineering
    assert "p.network_access" in renderer[:immutable_start]
    assert "networkLabel" in engineering
    assert "目前 HEAD" not in engineering
    assert "base_branch" not in engineering

    assert "p.base_branch" in legacy
    assert "目前 HEAD" in legacy
    for pinned_field in (
        "project_version_id",
        "base_commit",
        "execution_contract",
    ):
        assert pinned_field not in legacy


def test_identity_approval_kinds_have_summaries_and_unknown_payload_fallback():
    index = _read(INDEX_HTML)
    kind_labels = _between(index, "const KIND_LABEL = {", "};")
    identity_renderer = _javascript_function(index, "identityApprovalBodyHtml")
    payload_fallback = _javascript_function(
        index, "immutableApprovalPayloadDisclosureHtml"
    )
    overview_renderer = _javascript_function(index, "renderApprovals")
    chat_renderer = _javascript_function(index, "chatApprovalCardHtml")

    identity_contract = {
        "service_account_create": ("actor_id", "name", "description"),
        "service_token_issue": (
            "service_account_actor_id",
            "label",
            "scopes",
            "expires_at",
        ),
        "service_token_revoke": ("token_id",),
        "project_membership_upsert": ("project_id", "actor_id", "role"),
        "project_membership_remove": ("project_id", "actor_id"),
    }
    for kind, fields in identity_contract.items():
        assert kind in kind_labels
        assert f'kind === "{kind}"' in identity_renderer
        for field in fields:
            assert f"p.{field}" in identity_renderer
    assert "escapeHtml" in identity_renderer
    assert "JSON.stringify(p" not in identity_renderer

    assert "JSON.stringify" in payload_fallback
    assert "escapeHtml" in payload_fallback
    assert "immutable payload" in payload_fallback.lower()
    assert "<details" in payload_fallback

    for renderer in (overview_renderer, chat_renderer):
        assert "identityApprovalBodyHtml" in renderer
        assert "immutableApprovalPayloadDisclosureHtml" in renderer
        assert "尚無專用摘要" in renderer
    assert "escapeHtml(KIND_LABEL[a.kind] || a.kind" in overview_renderer


def test_service_token_issue_can_only_be_rejected_in_generic_approval_uis():
    index = _read(INDEX_HTML)
    identity_renderer = _javascript_function(index, "identityApprovalBodyHtml")
    overview_renderer = _javascript_function(index, "renderApprovals")
    chat_renderer = _javascript_function(index, "chatApprovalCardHtml")

    token_summary_start = identity_renderer.index(
        'if (kind === "service_token_issue")'
    )
    token_summary_end = identity_renderer.index(
        'if (kind === "service_token_revoke")', token_summary_start
    )
    token_summary = identity_renderer[token_summary_start:token_summary_end]
    assert "Token secret" in token_summary
    assert "response" in token_summary
    assert "出現一次" in token_summary
    assert "不會擷取 secret" in token_summary

    contracts = (
        (
            overview_renderer,
            'const actions = a.kind === "service_token_issue"',
            "approveApproval",
            "rejectApproval",
        ),
        (
            chat_renderer,
            'const actions = kind === "service_token_issue"',
            "chatApprove",
            "chatReject",
        ),
    )
    for renderer, marker, approve_handler, reject_handler in contracts:
        action_match = re.search(
            re.escape(marker)
            + r'\s*\?\s*`(?P<token>[^`]*)`\s*:\s*`(?P<ordinary>[^`]*)`;',
            renderer,
        )
        assert action_match is not None
        token_actions = action_match.group("token")
        ordinary_actions = action_match.group("ordinary")
        assert "disabled" in token_actions
        assert 'aria-disabled="true"' in token_actions
        assert "一次性 token secret" in token_actions
        assert approve_handler not in token_actions
        assert reject_handler in token_actions
        assert approve_handler in ordinary_actions
        assert reject_handler in ordinary_actions


def test_audit_events_use_semantic_collapsed_disclosure_rows():
    renderer = _javascript_function(_read(INDEX_HTML), "renderEvents")

    assert '<li><article class="audit-event">' in renderer
    assert "<time" in renderer
    assert "datetime=" in renderer
    assert "<details>" in renderer
    assert "<summary>" in renderer
    assert "<details open" not in renderer
    assert "JSON.stringify(params, null, 2)" in renderer
    assert "escapeHtml" in renderer
    assert "actor 未記錄" in renderer or "legacy" in renderer.lower()
    assert "e.result == null" in renderer
    assert "result 未記錄" in renderer
    assert "escapeHtml(e.result)" in renderer
    assert "${result}" in renderer


def test_unprobed_worker_state_remains_unknown_until_first_probe_timestamp():
    index = _read(INDEX_HTML)
    badge_renderer = _javascript_function(index, "serverBadge")
    table_renderer = _javascript_function(index, "renderServerConfigTable")

    badge_unknown = badge_renderer.index("server.updated_at == null")
    badge_offline = badge_renderer.index("server.online === false")
    assert badge_unknown < badge_offline
    assert 'cls: "unknown", label: "尚未探測"' in badge_renderer[
        badge_unknown:badge_offline
    ]

    table_unknown = table_renderer.index("state.updated_at == null")
    table_online = table_renderer.index("state.online === true")
    assert table_unknown < table_online
    assert '"\u5c1a\u672a\u63a2\u6e2c"' in table_renderer[table_unknown:table_online]
    assert '"\u96e2\u7dda"' not in table_renderer[table_unknown:table_online]


def test_runtime_job_rows_have_explicit_log_action_and_nonmodal_dialog_focus_lifecycle():
    index = _read(INDEX_HTML)
    renderer = _javascript_function(index, "renderJobs")
    show_panel = _javascript_function(index, "showLogPanel")
    close_panel = _javascript_function(index, "closeLogPanel")

    assert 'type="button"' in renderer
    assert "openLog(" in renderer
    assert "查看日誌" in renderer
    assert not re.search(r"<tr\b[^>]*\bonclick=", renderer)
    assert 'role="button"' not in renderer

    panel = _opening_tag(index, "div", "log-panel")
    assert 'role="dialog"' in panel
    assert 'aria-labelledby="log-title"' in panel
    assert 'id="log-title" tabindex="-1"' in index

    assert "document.activeElement" in show_panel
    assert "logPanelOpener" in show_panel
    assert 'classList.add("open")' in show_panel
    assert 'getElementById("log-title").focus()' in show_panel
    assert "restoreFocus" in close_panel
    assert "logPanelOpener" in close_panel
    assert ".isConnected" in close_panel
    assert ".focus(" in close_panel

    key_handler = _between(
        index,
        'document.getElementById("log-panel").addEventListener("keydown", (event) => {',
        "// ---- 失敗診斷",
    )
    assert 'event.key === "Escape"' in key_handler
    assert "event.preventDefault()" in key_handler
    assert "closeLogPanel({ restoreFocus: true })" in key_handler


def test_running_job_stop_warns_that_web_requests_may_execute_immediately():
    index = _read(INDEX_HTML)
    renderer = _javascript_function(index, "renderJobs")
    stopper = _between(
        index,
        "window.stopJob = async function",
        "window.rerunJob = async function",
    )

    assert "停止（可能立即執行）" in renderer
    assert "停止（需核准）" not in renderer
    confirm_at = stopper.index("window.confirm(")
    request_at = stopper.index("api(`/jobs/${id}/stop`")
    assert confirm_at < request_at
    assert "WEB_DIRECT_EXECUTE" in stopper
    assert "source: \"web\"" in stopper
    assert "自動核准並立即停止" in stopper
    assert "result.auto_approved" in stopper


def test_approval_fab_controls_a_named_panel_and_restores_focus_on_escape():
    index = _read(INDEX_HTML)
    trigger = _opening_tag(index, "button", "approval-fab")
    panel = _opening_tag(index, "div", "approval-panel")
    manager = _javascript_function(index, "setApprovalPanelOpen")

    assert 'aria-controls="approval-panel"' in trigger
    assert 'aria-expanded="false"' in trigger
    assert 'role="region"' in panel or 'role="dialog"' in panel
    assert 'aria-labelledby="approval-panel-title"' in panel
    assert 'aria-hidden="true"' in panel
    assert 'id="approval-panel-title" tabindex="-1"' in index

    assert 'setAttribute("aria-hidden"' in manager
    assert 'setAttribute("aria-expanded"' in manager
    assert 'getElementById("approval-panel-title").focus()' in manager
    assert "restoreFocus" in manager
    assert "trigger.focus()" in manager

    key_handler = _between(
        index,
        'document.getElementById("approval-panel").addEventListener("keydown", (event) => {',
        "// ---- 總覽：事件流",
    )
    assert 'event.key === "Escape"' in key_handler
    assert "event.preventDefault()" in key_handler
    assert "setApprovalPanelOpen(false, { restoreFocus: true })" in key_handler


def test_every_legacy_modal_opener_uses_the_shared_focus_manager():
    index = _read(INDEX_HTML)
    manager = _javascript_function(index, "openLegacyModal")

    assert 'document.getElementById("modal-backdrop").classList.add("open")' in manager
    assert 'modal.classList.add("open")' in manager
    assert "legacyModalOpener" in manager
    assert 'setAttribute("role", "dialog")' in manager
    assert 'setAttribute("aria-modal", "true")' in manager

    opener_contracts = {
        "server-modal": (
            'document.getElementById("new-server-btn").addEventListener',
            "window.openEditServerModal = function",
        ),
        "dataset-modal": (
            'document.getElementById("new-dataset-btn").addEventListener',
        ),
        "dataset-card-view-modal": ("window.openDatasetCardViewModal = async function",),
        "dataset-card-edit-modal": ("window.openDatasetCardEditModal = function",),
        "candidate-import-modal": ("window.openImportModal = function",),
        "git-init-modal": ("window.openGitInitModal = function",),
        "project-modal": (
            'document.getElementById("new-project-btn").addEventListener',
        ),
        "project-deploy-modal": ("window.openProjectDeployModal = function",),
        "record-modal": ("window.openRecordModal = function",),
        "dispatch-modal": ("window.openDispatchModal = function",),
        "coding-task-modal": ("window.openCodingTaskModal = function",),
    }
    markup_modal_ids = set(
        re.findall(r'<div\b[^>]*\bid="([^"]+-modal)"[^>]*\bclass="modal(?:\s|\")', index)
    )
    managed_modal_ids = set(re.findall(r'openLegacyModal\("([^"]+-modal)"', index))
    assert markup_modal_ids == set(opener_contracts)
    assert markup_modal_ids <= managed_modal_ids
    for modal_id, opener_markers in opener_contracts.items():
        expected_call = f'openLegacyModal("{modal_id}")'
        for marker in opener_markers:
            start = index.index(marker)
            window = index[start : start + 3500]
            assert expected_call in window, f"{marker} must open {modal_id} via manager"

    # The shared manager is the sole owner of the backdrop/open class pair.
    outside_manager = index.replace(manager, "")
    assert 'getElementById("modal-backdrop").classList.add("open")' not in outside_manager
    for modal_id in opener_contracts:
        assert not re.search(
            rf'getElementById\("{re.escape(modal_id)}"\)\.classList\.add\("open"\)',
            outside_manager,
        )


def test_every_static_form_control_has_a_programmatic_accessible_name():
    parser = _parse_static_html()
    missing: list[str] = []
    for line, tag, attributes, wrapped_by_label in parser.controls:
        if attributes.get("type") == "hidden":
            continue
        element_id = attributes.get("id")
        explicitly_named = any(
            attributes.get(attribute)
            for attribute in ("aria-label", "aria-labelledby", "title")
        )
        associated_label = bool(element_id and element_id in parser.labels_for)
        if not (wrapped_by_label or explicitly_named or associated_label):
            missing.append(f"line {line}: {tag}#{element_id or '(no id)'}")

    assert not missing, "controls without accessible names:\n" + "\n".join(missing)


def test_static_aria_and_label_targets_exist_and_dialogs_have_names():
    parser = _parse_static_html()
    id_counts = Counter(parser.ids)
    duplicate_ids = sorted(element_id for element_id, count in id_counts.items() if count > 1)
    assert not duplicate_ids, f"duplicate static IDs: {duplicate_ids}"

    known_ids = set(parser.ids)
    broken_references = [
        f"line {line}: {owner} {attribute} -> {target}"
        for line, owner, attribute, target in parser.references
        if target not in known_ids
    ]
    assert not broken_references, "broken static ARIA references:\n" + "\n".join(
        broken_references
    )
    missing_label_targets = sorted(parser.labels_for - known_ids)
    assert not missing_label_targets, f"label[for] targets missing: {missing_label_targets}"

    for line, dialog in parser.dialogs:
        labelledby = str(dialog.get("aria-labelledby") or "").split()
        assert dialog.get("aria-label") or labelledby, f"dialog at line {line} has no name"
        assert all(target in known_ids for target in labelledby)


def test_legacy_coding_run_dialog_handles_escape_and_restores_its_opener():
    index = _read(INDEX_HTML)
    panel = _opening_tag(index, "div", "coding-run-panel")
    opener = _between(
        index,
        "window.openCodingRunDetail = async function",
        'document.getElementById("coding-run-close-btn")',
    )
    closer = _javascript_function(index, "closeCodingRunPanel")

    assert 'role="dialog"' in panel
    assert 'aria-labelledby="coding-run-title"' in panel
    assert 'id="coding-run-title" tabindex="-1"' in index
    assert "document.activeElement" in opener
    assert "codingRunPanelOpener" in opener
    assert 'classList.add("open")' in opener
    assert 'getElementById("coding-run-title").focus()' in opener
    assert "restoreFocus" in closer
    assert "codingRunPanelOpener" in closer
    assert ".isConnected" in closer
    assert ".focus(" in closer

    key_handler = _between(
        index,
        'document.getElementById("coding-run-panel").addEventListener("keydown", (event) => {',
        "window.openValidationDispatchFromCodingRun",
    )
    assert 'event.key === "Escape"' in key_handler
    assert "event.preventDefault()" in key_handler
    assert "closeCodingRunPanel({ restoreFocus: true })" in key_handler


def test_mobile_layout_keeps_page_and_long_content_overflow_local():
    css = _read(UI_CSS)
    body_rule = _between(css, "body {", "}")
    table_rule = _between(css, "main table {", "}")
    long_content_rule = _between(
        css,
        "pre,\n.cmd,\n#log-panel pre,\n#coding-run-panel pre,\n.md-body pre.md-code {",
        "}",
    )
    supporting_nav_rule = _between(css, ".supporting-subnav {", "}")
    mobile = css[css.index("@media (max-width: 768px)") :]
    narrow_mobile = css[css.index("@media (max-width: 480px)") :]

    # Do not hide global overflow: that can mask clipped controls at 375px.
    # Width containment plus local scrollers must make overflow unnecessary.
    assert "overflow-x: hidden" not in body_rule
    assert "min-width: 0" in _between(css, ".app-frame {", "}")
    assert "min-width: 0" in _between(css, "section.tab-panel {", "}")
    assert "overflow-x: auto" in table_rule
    assert "max-width: 100%" in table_rule
    assert "overflow: auto" in long_content_rule
    assert "word-break: normal" in long_content_rule
    assert "overflow-x: auto" in supporting_nav_rule
    assert ".ui-dialog" in mobile and "width: 100vw" in mobile
    assert ".task-detail-tablist" in mobile
    assert ".supporting-toolbar" in narrow_mobile
    assert "flex-direction: column" in narrow_mobile


def test_interactive_touch_target_minimum_is_not_overridden_below_44px():
    css = _read(UI_CSS)
    base_controls = re.search(
        r"button,\s*input\[type=\"text\"\],\s*textarea,\s*select\s*"
        r"\{[^{}]*min-height:\s*44px;",
        css,
    )
    assert base_controls is not None

    violations: list[str] = []
    for match in re.finditer(r"(?P<selectors>[^{}]+)\{(?P<body>[^{}]*)\}", css):
        selectors = match.group("selectors").strip()
        body = match.group("body")
        if not re.search(
            r"\b(?:button|input|textarea|select)\b|\[role\s*=\s*[\"']?tab",
            selectors,
        ):
            continue
        for value in re.findall(r"min-height:\s*([^;]+);", body):
            pixels = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)px", value.strip())
            if pixels is None or float(pixels.group(1)) < 44:
                violations.append(f"{selectors}: min-height {value.strip()}")

    assert not violations, "interactive touch targets below 44px:\n" + "\n".join(
        violations
    )


def test_automated_dispatch_approval_kinds_have_summaries_in_both_renderers():
    """Goal 2 UI（Goal 3 Stage 1）：run_profile_*／dispatch_policy_*／
    auto_placement 七個 kind 都要有中文標籤與可讀摘要，且核准頁與聊天
    面板兩個渲染器走同一個摘要函式——不得退化成「尚無專用摘要」。"""

    index = _read(INDEX_HTML)
    kind_labels = _between(index, "const KIND_LABEL = {", "};")
    categories = _between(
        index, "const SUPPORTING_APPROVAL_CATEGORIES = ", "function supportingApprovalCategory"
    )
    body_renderer = _javascript_function(index, "automatedDispatchApprovalBodyHtml")
    overview_renderer = _javascript_function(index, "renderApprovals")
    chat_renderer = _javascript_function(index, "chatApprovalCardHtml")

    contract = {
        "run_profile_create": ("p.name", "p.command", "p.setup_cmd", "p.require_tag"),
        "run_profile_update": ("p.based_on_revision",),
        "run_profile_archive": ("p.name",),
        "dispatch_policy_create": (
            "p.allowed_servers",
            "p.run_profile_id",
            "p.dataset_required",
            "p.max_concurrent_placements",
            "p.valid_until",
        ),
        "dispatch_policy_update": ("p.based_on_revision",),
        "dispatch_policy_archive": ("p.name",),
        "auto_placement": (
            "p.server",
            "p.project",
            "p.command",
            "p.command_sha256",
            "p.policy_id",
            "p.policy_revision",
        ),
    }
    for kind, fields in contract.items():
        assert kind in kind_labels
        assert kind in categories
        assert f'"{kind}"' in body_renderer
        for field in fields:
            assert field in body_renderer
    assert "escapeHtml" in body_renderer
    # auto_placement 摘要必須誠實：核准會 enqueue、拒絕只影響本次提案。
    assert "enqueue" in body_renderer
    assert "冷卻" in body_renderer
    for renderer in (overview_renderer, chat_renderer):
        assert "automatedDispatchApprovalBodyHtml" in renderer


def test_infrastructure_idle_summary_surface_is_read_only_and_fail_closed():
    """基礎設施頁的閒置摘要（GET /servers/idle-summary）：唯讀觀測證據，
    未知不視為閒置；載入器要有 serial 防護與 loading/empty/error 狀態。"""

    workers = _section(
        _panel("tab-servers", "tab-coding-runs"),
        "infrastructure-workers",
        "infrastructure-inventory",
    )
    assert 'id="servers-idle-summary-card"' in workers
    assert 'id="servers-idle-summary"' in workers
    assert 'id="idle-summary-refresh-btn"' in workers
    assert "server_observations" in workers
    assert "fail-closed" in workers

    index = _read(INDEX_HTML)
    loader = _javascript_function(index, "loadIdleSummary")
    assert '"/servers/idle-summary"' in loader
    assert "idleSummaryLoadSerial" in loader
    for state in ('"loading"', '"empty"', '"error"'):
        assert state in loader
    assert "未知不等於閒置" in loader
    # 唯讀 surface：不得對任何端點做 POST/mutation。
    assert "POST" not in loader
    assert "/approve/" not in loader


def test_server_table_delete_entry_honestly_describes_real_removal():
    """伺服器列每列要有「刪除」入口（走 server_delete 核准流程），且確認
    對話必須誠實揭露後端語意。

    2026-07-26 起後端改為**真的移除**整筆設定，所以這裡釘的是新的誠實文案：
    必須說明會從 servers.yaml 移除、會先備份可還原、以及「只是不想派工請
    改用停用」。**這條測試的目的沒變**——不得讓使用者對按下去會發生什麼有
    錯誤預期（先前釘的是舊的軟刪除文案）。"""

    index = _read(INDEX_HTML)
    table_renderer = _javascript_function(index, "renderServerConfigTable")
    assert "deleteServerRequest(" in table_renderer
    assert "disableServerRequest(" in table_renderer

    handler_start = index.index("window.deleteServerRequest = async function")
    handler = index[handler_start : index.index("};", handler_start)]
    assert '"/server-config/delete-request"' in handler
    assert "window.confirm" in handler
    #: 必須明說是「移除」而不是停用，且不得殘留舊的「等同停用」說法。
    assert "移除" in handler
    assert "等同停用" not in handler
    assert "servers.yaml" in handler
    #: 必須告知可還原（有備份），以及指路到「停用」這個較輕的選項。
    assert "備份" in handler
    assert "停用" in handler
