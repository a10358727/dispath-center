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
    if not INDEX.is_file() or not CSS.is_file() or not JS.is_file():
        return ["static index.html/ui.css/ui.js assets are required"]
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
