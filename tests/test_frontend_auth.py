"""Source-level smoke tests for the dependency-free browser auth transition.

The frontend is a single checked-in HTML file and the repository intentionally
has no JavaScript test runtime.  These tests pin the security-sensitive wiring
in the same style as the existing frontend smoke tests without adding a browser
dependency or contacting an identity provider.
"""

from pathlib import Path


INDEX_HTML = Path(__file__).parents[1] / "static" / "index.html"
UI_JS = Path(__file__).parents[1] / "static" / "ui.js"


def _source() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def _ui_source() -> str:
    return UI_JS.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def test_header_has_current_user_oidc_logout_and_legacy_controls():
    source = _source()

    for element_id in (
        "current-user-status",
        "oidc-sign-in-btn",
        "logout-btn",
        "token-status",
        "set-token-btn",
    ):
        assert f'id="{element_id}"' in source


def test_current_actor_is_rendered_with_text_content_and_no_identity_claims():
    source = _source()
    renderer = _between(
        source,
        "function updateCurrentUserStatus(",
        "function setAuthenticationCheckingStatus(",
    )

    assert "status.textContent" in renderer
    assert "actor.display_name" in renderer
    assert ".innerHTML" not in renderer
    for forbidden in (
        "actor.email",
        "actor.issuer",
        "actor.subject",
        "session_id",
        "session_token",
    ):
        assert forbidden not in renderer


def test_role_badge_uses_safe_auth_me_metadata_without_becoming_an_action_gate():
    source = _ui_source()
    renderer = _between(
        source,
        "function updateIdentity(authInfo)",
        "function initialize()",
    )

    assert "actor.platform_admin" in renderer
    assert "authInfo.project_memberships" in renderer
    assert "actor.type" in renderer
    assert "badge.textContent" in renderer
    assert ".style.display" not in renderer
    for forbidden in (
        "actor.email",
        "actor.issuer",
        "actor.subject",
        "session_id",
        "session_token",
    ):
        assert forbidden not in renderer


def test_oidc_sign_in_preserves_only_same_origin_relative_browser_location():
    source = _source()
    sign_in = _between(
        source,
        "function sameOriginReturnTo()",
        'document.getElementById("logout-btn")',
    )

    assert "window.location.pathname" in sign_in
    assert "window.location.search" in sign_in
    assert "window.location.hash" in sign_in
    assert '!candidate.startsWith("/")' in sign_in
    assert 'candidate.startsWith("//")' in sign_in
    assert "new URLSearchParams({ return_to: sameOriginReturnTo() })" in sign_in
    assert "window.location.assign(`/auth/login?${params.toString()}`)" in sign_in


def test_auth_me_gates_initial_polling_and_websocket_connection():
    source = _source()
    initializer = _between(
        source,
        "async function initializeBrowserAuthentication()",
        "// ---- 分頁切換",
    )
    footer = _between(
        source,
        'window.addEventListener("hashchange", applyRoute);',
        "</script>",
    )

    auth_me_at = initializer.index('fetch("/auth/me"')
    enable_at = initializer.index("applicationRequestsEnabled = true")
    websocket_at = initializer.index("connectChatSocket()")
    poll_at = initializer.index("pollTimer = setInterval(refreshAll, 5000)")
    assert auth_me_at < enable_at < websocket_at < poll_at
    assert 'if (response.status === 401)' in initializer
    assert "activateTab(\"overview\");\n    return;" in initializer
    assert footer.count("initializeBrowserAuthentication();") == 1
    assert "connectChatSocket();" not in footer
    assert "refreshAll();" not in footer
    assert "setInterval(refreshAll" not in footer


def test_oidc_availability_comes_from_safe_auth_me_body_or_401_header():
    source = _source()
    resolver = _between(
        source,
        "function oidcEnabledFromResponse(",
        "function updateCurrentUserStatus(",
    )

    assert "body.oidc_enabled" in resolver
    assert 'response.headers.get("X-OIDC-Enabled")' in resolver


def test_later_401_stops_polling_websocket_and_clears_auth_state():
    source = _source()
    handler = _between(
        source,
        "function handleUnauthorizedResponse(",
        "function sameOriginReturnTo(",
    )
    api = _between(source, "async function api(", "async function initializeBrowserAuthentication(")

    assert "clearLegacyToken()" in handler
    assert "stopProtectedActivity()" in handler
    assert "clearProtectedUi()" in handler
    assert "updateCurrentUserStatus(null" in handler
    assert "authInitializationSerial += 1" in handler
    assert "requestAuthSerial !== authInitializationSerial" in api
    assert "handleUnauthorizedResponse(res)" in api
    assert '{ credentials: "same-origin" }' in api
    assert "oidcAvailable || oidcEnabledFromResponse(response, null)" in handler


def test_authenticated_patch_download_preserves_auth_and_generation_guards():
    source = _source()
    helper = _between(
        source,
        "function sameOriginDownloadPath(path)",
        "async function initializeBrowserAuthentication()",
    )

    assert '!path.startsWith("/")' in helper
    assert 'path.startsWith("//")' in helper
    assert "parsed.origin !== window.location.origin" in helper
    assert "normalized !== path" in helper
    assert "const requestAuthSerial = authInitializationSerial" in helper
    assert 'headers["X-Auth-Token"] = authToken' in helper
    assert 'credentials: "same-origin"' in helper
    assert 'cache: "no-store"' in helper
    assert "requestAuthSerial !== authInitializationSerial" in helper
    assert "response.status === 401" in helper
    assert "handleUnauthorizedResponse(response)" in helper
    assert 'contentType.startsWith("application/json")' in helper
    assert 'typeof body.detail === "string"' in helper
    assert 'response.headers.get("X-Engineering-Patch-Redacted")' in helper
    assert 'redactedHeader !== "true" && redactedHeader !== "false"' in helper
    assert 'response.headers.get("X-Artifact-Semantics")' in helper
    assert 'artifactSemantics !== "sanitized-collected-patch"' in helper
    assert 'responseContentType !== "text/x-diff"' in helper
    assert "await response.blob()" in helper
    assert "blob.size < 1 || blob.size > 1024 * 1024" in helper
    assert "Content-Disposition" not in helper


def test_logout_closes_ws_clears_legacy_token_posts_and_reloads():
    source = _source()
    logout = _between(
        source,
        'document.getElementById("logout-btn")',
        "function showToast(",
    )

    assert 'stopProtectedActivity("已登出")' in logout
    assert "clearLegacyToken()" in logout
    assert 'fetch("/auth/logout"' in logout
    assert 'method: "POST"' in logout
    assert 'credentials: "same-origin"' in logout
    assert 'window.location.replace("/")' in logout


def test_legacy_http_and_websocket_fallback_wiring_is_retained():
    source = _source()

    assert 'localStorage.getItem("auth_token")' in source
    assert 'localStorage.setItem("auth_token", authToken)' in source
    assert 'headers["X-Auth-Token"] = authToken' in source
    assert 'ws.send(JSON.stringify({ type: "auth", token: authToken }))' in source
    assert 'new WebSocket(`${proto}//${window.location.host}/ws`)' in source
    assert "/ws?token=" not in source


def test_auth_transition_clears_all_cross_principal_ui_state():
    source = _source()
    clearer = _between(
        source,
        "function clearProtectedUi()",
        "function stopProtectedActivity(",
    )

    for required in (
        "currentAuthFingerprint = null",
        "codingRunDetailCache = null",
        "projectsMatrixCache = null",
        "projectDetailCurrent = null",
        "projectDetailData = null",
        "timelineItems = []",
        'document.getElementById("chat-messages").textContent = ""',
        'document.getElementById("log-panel").classList.remove("open")',
        'document.getElementById("coding-run-panel").classList.remove("open")',
        # Never restore focus into controls that belonged to the previous
        # authenticated principal while clearing cross-principal state.
        "closeAllModals({ restoreFocus: false })",
        'document.querySelectorAll(".modal input, .modal textarea")',
        'document.getElementById("dataset-card-view-rendered").textContent = ""',
    ):
        assert required in clearer


def test_websocket_callbacks_are_bound_to_active_auth_generation():
    source = _source()
    websocket = _between(source, "function connectChatSocket()", "function stopChatSocket(")

    assert "const socketAuthSerial = authInitializationSerial" in websocket
    assert websocket.count("chatSocket !== ws") >= 3
    assert websocket.count("socketAuthSerial !== authInitializationSerial") >= 3
    assert "!applicationRequestsEnabled" in websocket
    assert "if (chatSocket === ws && socketAuthSerial === authInitializationSerial)" in websocket


def test_periodic_auth_check_precedes_protected_refresh_and_reinitializes_actor_switch():
    source = _source()
    refresh = _between(source, "async function refreshAll()", "// 專案詳情頁計畫")
    initializer = _between(
        source,
        "async function initializeBrowserAuthentication()",
        "// ---- 分頁切換",
    )

    auth_at = refresh.index('const authInfo = await api("/auth/me")')
    data_at = refresh.index("await Promise.all([")
    assert auth_at < data_at
    assert "refreshedFingerprint !== currentAuthFingerprint" in refresh
    assert "initializeBrowserAuthentication();\n      return;" in refresh
    assert "currentAuthFingerprint = authenticationFingerprint(authInfo)" in initializer
    assert "if (serial !== authInitializationSerial) return;\n    clearProtectedUi();" in initializer
