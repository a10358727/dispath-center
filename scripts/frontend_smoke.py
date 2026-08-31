"""Dependency-free frontend build/smoke gate.

DG-STUDIO-UI v1 P3-4 (root cutover): the v2 Workspace
(`workspace.html`/`workspace.css`/`workspace.js`/`workspace-features.js`) is
retired and deleted.  The checked static surface is now `static/login.html`
(the login-first root an unauthenticated visitor sees) plus the gitignored
Studio SPA build under `static/studio/` (`GET /` serves it once signed in).
This gate validates that boundary: the login page stays script-free and
off-origin-free, the Studio build is self-contained under `/static/studio/`,
and the retired Workspace/legacy assets never come back.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
#: Login-first root: `GET /` serves this standalone page to an
#: unauthenticated visitor instead of the Studio shell.
LOGIN = ROOT / "static" / "login.html"
#: DG-UI-UNIFICATION v1 U8 retired the legacy `index.html`/`ui.js` surface;
#: DG-STUDIO-UI v1 P3-4 retired the v2 Workspace the same way. Neither may
#: reappear -- `GET /` has exactly two authenticated answers (Studio build or
#: the inline build-me notice) and one unauthenticated answer (login.html).
RETIRED_UI_ASSETS = (
    "index.html",
    "ui.css",
    "ui.js",
    "workspace.html",
    "workspace.css",
    "workspace.js",
    "workspace-features.js",
)
#: DG-STUDIO-UI v1: the Studio SPA build lands here (gitignored). When present it
#: must be self-contained: every script/stylesheet is a same-origin asset under
#: `/static/studio/` -- never a remote URL (same posture as the Workspace).
STUDIO_DIR = ROOT / "static" / "studio"
STUDIO_ASSET_PREFIX = "/static/studio/"


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


def check_studio(studio_dir: Path) -> list[str]:
    """Validate a Studio build directory (no-op list when it is absent)."""

    index = studio_dir / "index.html"
    if not index.is_file():
        return []
    errors: list[str] = []
    parser = _AssetParser()
    parser.feed(index.read_text(encoding="utf-8"))
    if not parser.scripts:
        errors.append("static/studio/index.html must reference at least one built script")
    for reference in parser.scripts + parser.stylesheets:
        if not reference.startswith(STUDIO_ASSET_PREFIX):
            errors.append(f"static/studio/index.html must only load assets under {STUDIO_ASSET_PREFIX}: {reference}")
            continue
        if not (studio_dir / reference[len(STUDIO_ASSET_PREFIX):]).is_file():
            errors.append(f"static/studio/index.html references a missing asset: {reference}")
    for asset in sorted((studio_dir / "assets").glob("*.js")) if (studio_dir / "assets").is_dir() else []:
        text = asset.read_text(encoding="utf-8", errors="replace")
        if re.search(r"""import\(\s*["']https?://""", text) or re.search(r"""new\s+WebSocket\(\s*["']wss?://""", text):
            errors.append(f"{asset.name} must not hard-code a remote import or WebSocket origin")
    return errors


def check(*, require_studio: bool = False) -> list[str]:
    errors: list[str] = []
    if require_studio and not (STUDIO_DIR / "index.html").is_file():
        errors.append("static/studio/index.html is required (run `npm ci && npm run build` in studio/)")
    errors.extend(check_studio(STUDIO_DIR))

    for name in RETIRED_UI_ASSETS:
        if (ROOT / "static" / name).exists():
            errors.append(f"retired UI asset must not return: static/{name}")

    #: Login-first root: `static/login.html` must exist, must carry the OIDC
    #: entry point, and -- being the one page an unauthenticated visitor ever
    #: sees -- must not run any script or reach off-origin.
    if not LOGIN.is_file():
        errors.append("static/login.html (login-first root page) is required")
    else:
        login = LOGIN.read_text(encoding="utf-8")
        if "使用 OIDC 登入" not in login:
            errors.append("login.html must offer a 「使用 OIDC 登入」 entry point")
        if "https://" in login or "http://" in login:
            errors.append("login.html must not load remote URLs")
        for storage in ("localStorage", "sessionStorage", "indexedDB"):
            if storage in login:
                errors.append(f"login.html must not use browser storage: {storage}")
        login_parser = _AssetParser()
        login_parser.feed(login)
        if login_parser.scripts:
            errors.append("login.html must not reference any <script src=...>")
        if "<script" in login:
            errors.append("login.html must not contain an inline <script>")
    return errors


def main() -> int:
    errors = check(require_studio="--require-studio" in sys.argv[1:])
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("frontend smoke: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
