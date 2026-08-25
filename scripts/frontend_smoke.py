"""Dependency-free frontend build/smoke gate.

The UI is intentionally shipped as checked-in static assets rather than a
Node package.  This gate validates the asset boundary, required workflow
anchors, and absence of remote script/style dependencies; JavaScript syntax is
checked separately by CI when Node is available.

DG-UI-UNIFICATION v1 U8: the legacy `static/index.html`/`ui.css`/`ui.js`
surface is retired -- the v2 Workspace (`workspace.html`/`workspace.css`/
`workspace.js`/`workspace-features.js`) is now the sole checked asset set.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "static" / "workspace.html"
WORKSPACE_CSS = ROOT / "static" / "workspace.css"
WORKSPACE_JS = ROOT / "static" / "workspace.js"
WORKSPACE_FEATURES_JS = ROOT / "static" / "workspace-features.js"


class _AssetParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[str] = []
        self.stylesheets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and attributes.get("src"):
            self.scripts.append(str(attributes["src"]))
        if tag == "link" and attributes.get("rel") == "stylesheet":
            href = attributes.get("href")
            if href:
                self.stylesheets.append(str(href))


def check() -> list[str]:
    errors: list[str] = []
    if not all(
        path.is_file()
        for path in (WORKSPACE, WORKSPACE_CSS, WORKSPACE_JS, WORKSPACE_FEATURES_JS)
    ):
        return ["Product v2 Workspace static UI assets are required"]

    workspace = WORKSPACE.read_text(encoding="utf-8")
    workspace_javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    #: DG-UI-UNIFICATION v1 U1 (docs/DECISIONS.md 2026-08-25): a second IIFE,
    #: `workspace-features.js`, was added alongside `workspace.js` (same
    #: `window.WorkspaceUI` hand-off pattern the retired legacy `ui.js` used
    #: for `window.DispatchUI`). This pin documents the script count;
    #: `workspace-features.js` loads first (dependency direction:
    #: `workspace.js` calls into `window.WorkspaceUI` at render time).
    workspace_features_javascript = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    workspace_parser = _AssetParser()
    workspace_parser.feed(workspace)
    if len(workspace_parser.scripts) != 2:
        errors.append(
            "workspace.html must reference exactly two versioned scripts "
            "(workspace-features.js, workspace.js)"
        )
    else:
        if not re.fullmatch(
            r"/static/workspace-features\.js\?v=[A-Za-z0-9._-]+",
            workspace_parser.scripts[0],
        ):
            errors.append(
                "workspace.html must reference one versioned workspace-features.js "
                "before workspace.js"
            )
        if not re.fullmatch(
            r"/static/workspace\.js\?v=[A-Za-z0-9._-]+",
            workspace_parser.scripts[1],
        ):
            errors.append("workspace.html must reference one versioned workspace.js")
    if len(workspace_parser.stylesheets) != 1 or not re.fullmatch(
        r"/static/workspace\.css\?v=[A-Za-z0-9._-]+",
        workspace_parser.stylesheets[0],
    ):
        errors.append("workspace.html must reference one versioned workspace.css")
    for marker in (
        'id="workspace-navigation"',
        'data-workspace-section="overview"',
        'data-workspace-section="project-bootstrap"',
        'data-role-navigation="approval"',
        'id="bootstrap-form"',
        'id="dataset-publish-panel"',
        'id="dataset-publish-local-path"',
        'id="run-detail-panel"',
        'id="run-clone-btn"',
        'id="run-stop-btn"',
        'id="run-artifact-list"',
        'id="run-compare-btn"',
        'id="approval-review-confirm"',
        'const PRODUCT_READ_PATHS = new Set([',
        'const PRODUCT_MUTATION_PATHS = new Set([',
        'function bootstrapRequestBody()',
        'function datasetPublishRequestBody()',
        'async function previewDatasetPublish(',
        'async function requestDatasetPublish()',
        'function loadApprovalDetail(',
        'async function decideReviewedApproval(',
        '"dataset_publish_v2"',
        'const DATASET_PUBLISH_MUTATION_PATH = ',
        'function renderProjectWorkspace()',
        'function renderRunDetail()',
        'async function previewRunClone()',
        'async function requestRunStop()',
        'async function compareRuns()',
        'const PRODUCT_RUN_MUTATION_PATH = ',
        #: DG-UI-UNIFICATION v1 U3: jobs/runtime section markers.
        'data-workspace-section="jobs"',
        'id="jobs-tbody"',
        #: DG-UI-UNIFICATION v1 U4: infrastructure section markers.
        'data-workspace-section="infrastructure"',
        'id="infra-workers-tbody"',
        #: DG-UI-UNIFICATION v1 U5: projects/datasets section markers.
        'data-workspace-section="legacy-datasets"',
        'id="legacy-matrix-tbody"',
        #: DG-UI-UNIFICATION v1 U6a: AI 工程 section (task list/detail/
        #: wizard) markers.
        'data-workspace-section="engineering"',
        'id="engineering-tasks-tbody"',
        'id="engineering-wizard-panel"',
        'id="engineering-task-detail-panel"',
        'data-task-tab="overview"',
        'async function loadEngineeringTasks()',
        'const ENGINEERING_TASK_READ_PATH = ',
        'const ENGINEERING_TASK_MUTATION_PATH = ',
        'async function authenticatedEngineeringPatchDownload(',
        #: DG-UI-UNIFICATION v1 U6b: AgentSession/AI conversation markers.
        'id="legacy-agent-session-section"',
        'id="legacy-ai-conversation-section"',
        #: DG-UI-UNIFICATION v1 U7: 助手（chat assistant）section markers.
        'data-section="assistant"',
        'data-workspace-section="assistant"',
        'id="assistant-messages"',
        'id="assistant-form"',
        'const CHAT_WEBSOCKET_PATH = ',
        'function chatConnect(',
        'function chatSend(',
        #: DG-UI-UNIFICATION v1 U8: 總覽整併 markers (worker health / activity
        #: & audit / administration entry points).
        'id="overview-server-cards"',
        'id="overview-activity-list"',
        'id="overview-administration-links"',
        'async function loadOverviewServers()',
        'async function loadOverviewActivity()',
    ):
        if marker not in workspace and marker not in workspace_javascript:
            errors.append(f"missing Product v2 workspace marker: {marker}")
    #: DG-UI-UNIFICATION v1 U1: `workspace-features.js` markers — the ported
    #: Chinese kind labels, the `window.WorkspaceUI` hand-off (mirrors the
    #: retired legacy `ui.js`'s `window.DispatchUI`), and the summary
    #: renderer.
    for marker in (
        "const KIND_LABEL = Object.freeze({",
        "const STATUS_LABEL = Object.freeze({",
        "const ONE_TIME_SECRET_APPROVAL_KINDS = Object.freeze(",
        "window.WorkspaceUI = Object.freeze({",
        "function buildApprovalSummaryNodes(",
        "function renderApprovalSummary(",
        "function approvalCategoryLabel(",
        #: DG-UI-UNIFICATION v1 U6a: the byte-for-byte ported instruction
        #: renderer + its limit constant (machine contract, pinned).
        "function renderEngineeringTaskInstruction(",
        "const ENGINEERING_INSTRUCTION_LIMIT = ",
    ):
        if marker not in workspace_features_javascript:
            errors.append(f"missing workspace-features.js marker: {marker}")
    if "https://" in workspace or "http://" in workspace:
        errors.append("Product v2 frontend assets must not load remote URLs")
    if "node_modules" in workspace_javascript or "require(" in workspace_javascript:
        errors.append("dependency-free workspace.js must not require node modules")
    if "node_modules" in workspace_features_javascript or "require(" in workspace_features_javascript:
        errors.append("dependency-free workspace-features.js must not require node modules")
    if "https://" in workspace_features_javascript or "http://" in workspace_features_javascript:
        errors.append("workspace-features.js must not load remote URLs")
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        if (
            storage in workspace
            or storage in workspace_javascript
            or storage in workspace_features_javascript
        ):
            errors.append(f"browser credential persistence is forbidden: {storage}")
    return errors


def main() -> int:
    errors = check()
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("frontend smoke: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
