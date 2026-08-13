"""Dependency-free frontend build/smoke gate.

The UI is intentionally shipped as checked-in static assets rather than a
Node package.  This gate validates the asset boundary, required workflow
anchors, and absence of remote script/style dependencies; JavaScript syntax is
checked separately by CI when Node is available.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "static" / "index.html"
CSS = ROOT / "static" / "ui.css"
JS = ROOT / "static" / "ui.js"
WORKSPACE = ROOT / "static" / "workspace.html"
WORKSPACE_CSS = ROOT / "static" / "workspace.css"
WORKSPACE_JS = ROOT / "static" / "workspace.js"


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
        for path in (INDEX, CSS, JS, WORKSPACE, WORKSPACE_CSS, WORKSPACE_JS)
    ):
        return ["legacy and Product v2 static UI assets are required"]
    index = INDEX.read_text(encoding="utf-8")
    javascript = JS.read_text(encoding="utf-8")
    parser = _AssetParser()
    parser.feed(index)
    if len(parser.scripts) != 1 or not re.fullmatch(
        r"/static/ui\.js\?v=[A-Za-z0-9._-]+", parser.scripts[0]
    ):
        errors.append("index.html must reference exactly one versioned /static/ui.js")
    if len(parser.stylesheets) != 1 or not re.fullmatch(
        r"/static/ui\.css\?v=[A-Za-z0-9._-]+", parser.stylesheets[0]
    ):
        errors.append("index.html must reference exactly one versioned /static/ui.css")
    if "https://" in index or "http://" in index:
        errors.append("frontend assets must not load remote URLs")
    required_markers = (
        'id="project-subnavigation"',
        'id="approval-fab"',
        'data-project-workspace-pane="overview"',
        'data-project-workspace-pane="runs-validation"',
        'data-project-workspace-pane="deployment"',
        "function openEngineeringTaskWizard(",
        "function renderApprovals(",
        "function renderTimelineList(",
    )
    for marker in required_markers:
        if marker not in index and marker not in javascript:
            errors.append(f"missing workflow marker: {marker}")
    if "node_modules" in javascript or "require(" in javascript:
        errors.append("dependency-free ui.js must not require node modules")

    workspace = WORKSPACE.read_text(encoding="utf-8")
    workspace_javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    workspace_parser = _AssetParser()
    workspace_parser.feed(workspace)
    if len(workspace_parser.scripts) != 1 or not re.fullmatch(
        r"/static/workspace\.js\?v=[A-Za-z0-9._-]+",
        workspace_parser.scripts[0],
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
    ):
        if marker not in workspace and marker not in workspace_javascript:
            errors.append(f"missing Product v2 workspace marker: {marker}")
    if "https://" in workspace or "http://" in workspace:
        errors.append("Product v2 frontend assets must not load remote URLs")
    if "node_modules" in workspace_javascript or "require(" in workspace_javascript:
        errors.append("dependency-free workspace.js must not require node modules")
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        if storage in index or storage in javascript or storage in workspace_javascript:
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
