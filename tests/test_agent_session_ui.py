"""DG-AGENT-SESSION-V1 P4 (docs/product/AGENT_SESSION_V1_PLAN.md §5 P4):
static contracts for the Development Session workbench added to the
"AI 工程" pane. These are presentation-only assertions on the checked-in
`static/index.html`/`static/ui.js` -- no browser runtime, no backend calls --
mirroring the pattern already used for the CV-2a AI Engineer conversation
pane (`tests/test_project_conversation.py`) and the run-request panel
(`tests/test_project_workspace_ui.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "static" / "index.html"
UI_JS = ROOT / "static" / "ui.js"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    end_at = source.index(end, start_at)
    return source[start_at:end_at]


def _javascript_function(source: str, name: str) -> str:
    """Return one named JS function while ignoring braces inside
    strings/comments (duplicated from `tests/test_project_workspace_ui.py`
    per this repo's existing per-file-helper convention)."""

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


def _ai_engineering_pane() -> str:
    html = _read(INDEX_HTML)
    start = html.index('data-project-workspace-pane="ai-engineering"')
    end = html.index("</section>", html.index("</section>", start) + 1)
    return html[start:end]


def test_agent_session_markup_present_and_hidden_by_default_above_chat():
    pane = _ai_engineering_pane()

    # Composition choice: the session section appears BEFORE (above) the
    # CV-2a conversation section in source order -- least-surgery choice,
    # both panes coexist rather than a tab-toggle state machine.
    session_at = pane.index('id="pd-agent-session-section"')
    conversation_at = pane.index('id="pd-ai-conversation-section"')
    assert session_at < conversation_at

    for element_id in (
        "pd-agent-session-section",
        "pd-agent-session-refresh-btn",
        "pd-agent-session-state",
        "pd-agent-session-open-panel",
        "pd-agent-session-open-form",
        "pd-agent-session-version",
        "pd-agent-session-open-btn",
        "pd-agent-session-pending-panel",
        "pd-agent-session-pending-text",
        "pd-agent-session-workbench",
        "pd-agent-session-turns",
        "pd-agent-session-close-btn",
        "pd-agent-session-messages",
        "pd-agent-session-form",
        "pd-agent-session-input",
        "pd-agent-session-send-btn",
        "pd-agent-session-status",
        "pd-agent-session-turn-panel",
        "pd-agent-session-turn-status",
        "pd-agent-session-events",
        "pd-agent-session-diff-btn",
        "pd-agent-session-diff",
    ):
        assert f'id="{element_id}"' in pane, f"missing #{element_id}"

    # Hidden by default until the GET .../agent-sessions probe confirms the
    # flag is on and resolves a state (CV-6 precedent: flag off hides UI,
    # never shows a "feature disabled" card).
    section_tag = re.search(
        r'<section\b[^>]*\bid="pd-agent-session-section"[^>]*>', pane
    )
    assert section_tag is not None
    assert "hidden" in section_tag.group(0)

    # The three mutually-exclusive sub-panels also start hidden; JS toggles
    # exactly one of them on via `agentSessionShowPanel`.
    for panel_id in (
        "pd-agent-session-open-panel",
        "pd-agent-session-pending-panel",
        "pd-agent-session-workbench",
    ):
        opening = re.search(rf'<div\b[^>]*\bid="{panel_id}"[^>]*>', pane)
        assert opening is not None
        assert "hidden" in opening.group(0)


def test_checkpoint_and_promote_controls_present_dg_agent_session_checkpoint():
    """DG-AGENT-SESSION-CHECKPOINT (docs/DECISIONS.md 2026-08-24: A 核准),
    superseding the P4 TODO this test previously pinned (checkpoint/promote
    had no backend yet): the session workbench now has a "建立 Checkpoint"
    button and an initially-hidden "Promote" button, both request-only --
    decision-making stays on the Approvals page (UX addendum)."""

    pane = _ai_engineering_pane()
    assert 'id="pd-agent-session-checkpoint-btn"' in pane
    assert 'id="pd-agent-session-promote-btn"' in pane
    assert re.search(r'id="pd-agent-session-promote-btn"[^>]*\bhidden\b', pane)
    assert 'id="pd-agent-session-checkpoint-status"' in pane

    js = _read(UI_JS)
    for name in (
        "submitAgentSessionCheckpoint",
        "submitAgentSessionPromote",
        "agentSessionCheckpointPollOnce",
        "agentSessionPromotePollOnce",
        "agentSessionUpdateCheckpointButtons",
    ):
        assert re.search(rf"\bfunction\s+{name}\s*\(", js), f"missing {name}"
    # Checkpoint-request and promote-request each go through their own
    # existing/new REST route -- never a fabricated/auto-chained call.
    assert "/checkpoint-request" in js
    assert "/promote-request" in js
    # Poll loops read approval state via the existing generic list route
    # (no new bridge-lookup read surface) -- see module docstring on why.
    assert "/approvals?kind=agent_session_checkpoint" in js
    assert "/approvals?kind=engineering_task_promote" in js
    # Never auto-chained: creating a checkpoint/promote request always
    # requires an explicit user click behind a confirm() dialog.
    checkpoint_fn = _javascript_function(js, "submitAgentSessionCheckpoint")
    assert "window.confirm(" in checkpoint_fn
    promote_fn = _javascript_function(js, "submitAgentSessionPromote")
    assert "window.confirm(" in promote_fn


def test_agent_session_panel_functions_exist_and_are_exported():
    js = _read(UI_JS)
    for name in (
        "resetAgentSessionPanel",
        "loadAgentSessionPanel",
        "submitAgentSessionOpen",
        "submitAgentSessionMessage",
        "agentSessionCloseCurrent",
        "loadAgentSessionDiff",
        "agentSessionPollOnce",
        "agentSessionStartTurnPolling",
        "agentSessionStopPolling",
        "agentSessionParseTranscriptLine",
        "agentSessionRenderTranscriptChunk",
        "agentSessionRenderDiff",
    ):
        assert re.search(rf"\bfunction\s+{name}\s*\(", js), f"missing {name}"

    assert "loadAgentSessionPanel," in js
    assert "resetAgentSessionPanel," in js


def test_agent_session_wired_into_open_project_detail_reset_and_load():
    html = _read(INDEX_HTML)
    assert "window.DispatchUI.resetAgentSessionPanel();" in html
    assert "window.DispatchUI.loadAgentSessionPanel(name);" in html

    # Reset/load ordering matches the existing run-request/conversation
    # panels: reset happens before the detail fetch, load happens after.
    reset_block = _between(html, "if (window.DispatchUI) window.DispatchUI.resetRunRequestPanel();", "loadProjectEngineeringTasks(name, openSerial);")
    assert "resetAgentSessionPanel" in reset_block
    load_block = _between(html, "if (window.DispatchUI) window.DispatchUI.loadRunRequestPanel(name);", "} catch (e) {")
    assert "loadAgentSessionPanel" in load_block


def test_open_form_reuses_promoted_version_dropdown_source():
    js = _read(UI_JS)
    loader = _javascript_function(js, "agentSessionLoadOpenForm")
    assert "/versions`" in loader
    assert 'promotion_state === "promoted"' in loader
    assert "promotedVersions.forEach" in loader
    assert "尚無 promoted 版本" in loader


def test_open_request_flow_creates_pending_state_never_auto_approves():
    js = _read(UI_JS)
    opener = _javascript_function(js, "submitAgentSessionOpen")
    assert "/agent-sessions/open-request" in opener
    assert "base_version_id" in opener
    assert "/approve/" not in opener
    assert "pendingApprovalId" in opener

    guidance = _javascript_function(js, "agentSessionRenderPendingGuidance")
    assert "核准" in guidance
    assert "agent_session_open" in guidance


def test_message_send_flow_references_messages_endpoint_and_64kib_cap():
    js = _read(UI_JS)
    sender = _javascript_function(js, "submitAgentSessionMessage")
    assert "/agent-sessions/${encodeURIComponent(session.id)}/messages" in sender
    assert "AGENT_SESSION_MESSAGE_MAX_BYTES" in sender
    assert 'result.status === "unreachable"' in sender
    assert "65536" in js  # client-side mirror of the server 64 KiB cap


def test_transcript_poller_references_transcript_endpoint_and_has_serial_guard():
    js = _read(UI_JS)
    poller = _javascript_function(js, "agentSessionPollOnce")
    assert "/agent-sessions/${encodeURIComponent(sessionId)}/transcript?turn=" in poller
    assert "offset=" in poller
    # Serial guard: bail out before doing any work / scheduling the next
    # poll if a newer pane load/turn invalidated this loop.
    assert poller.count("serial !== agentSessionState.pollSerial") >= 2

    starter = _javascript_function(js, "agentSessionStartTurnPolling")
    assert "agentSessionStopPolling" in starter

    stopper = _javascript_function(js, "agentSessionStopPolling")
    assert "pollSerial += 1" in stopper
    assert "clearTimeout" in stopper


def test_jsonl_lines_are_parsed_defensively_inside_try_catch():
    js = _read(UI_JS)
    parser = _javascript_function(js, "agentSessionParseTranscriptLine")
    assert "try {" in parser
    assert "JSON.parse(line)" in parser
    assert "catch (error)" in parser
    assert "return null" in parser

    chunk_renderer = _javascript_function(js, "agentSessionRenderTranscriptChunk")
    assert "agentSessionParseTranscriptLine(line)" in chunk_renderer
    assert "tool_use" in chunk_renderer
    assert "tool_result" in chunk_renderer
    assert "\U0001F527" in chunk_renderer  # the "🔧 " tool-call prefix


def test_transcript_and_event_rendering_never_uses_innerhtml():
    js = _read(UI_JS)
    for name in (
        "agentSessionRenderTranscriptChunk",
        "agentSessionAppendEvent",
        "agentSessionMakeMessageItem",
        "agentSessionRenderMessagesList",
        "agentSessionAppendMessage",
        "agentSessionRenderDiff",
        "agentSessionRenderPendingGuidance",
    ):
        body = _javascript_function(js, name)
        assert ".innerHTML" not in body, f"{name} must not assign innerHTML"


def test_diff_panel_wired_to_diff_endpoint_and_reuses_engineering_task_pattern():
    js = _read(UI_JS)
    loader = _javascript_function(js, "loadAgentSessionDiff")
    assert "/agent-sessions/${encodeURIComponent(sessionId)}/diff" in loader

    renderer = _javascript_function(js, "agentSessionRenderDiff")
    # Same response contract as `GET /engineering-tasks/{task_id}/diff`
    # (available/status/patch/truncated/redacted/withheld) plus the
    # additive dirty/untracked file lists.
    assert "response.withheld" in renderer
    assert 'response.status === "no_workspace"' in renderer
    assert 'response.status === "unreachable"' in renderer
    assert "response.available" in renderer
    assert "response.patch" in renderer
    assert "task-diff-viewer" in renderer  # same CSS class as the engineering-task viewer
    assert "dirty_files" in renderer
    assert "untracked_files" in renderer
    assert ".innerHTML" not in renderer


def test_close_session_requires_confirm_and_never_auto_closes():
    js = _read(UI_JS)
    closer = _javascript_function(js, "agentSessionCloseCurrent")
    assert "window.confirm(" in closer
    assert "/close`" in closer
    assert "{ method: \"POST\" }" in closer


def test_degraded_states_rendered_as_readable_text_not_thrown():
    js = _read(UI_JS)
    poller = _javascript_function(js, "agentSessionPollOnce")
    assert '"unreachable"' in poller
    assert "Runner 暫時無法連線" in poller

    sender = _javascript_function(js, "submitAgentSessionMessage")
    assert "無法連線到 Runner" in sender

    workbench_renderer = _javascript_function(js, "agentSessionRenderWorkbench")
    assert "已達 session turn 上限" in workbench_renderer
