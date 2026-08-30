"""DG-STUDIO-UI v1: the Studio SPA build is served under the public `/static/`
prefix (no new auth exemption) and must be self-contained."""

from pathlib import Path

import pytest

from scripts.frontend_smoke import check_studio

ROOT = Path(__file__).resolve().parents[1]
STUDIO_BUILD = ROOT / "static" / "studio" / "index.html"


def _write_build(root: Path, index_html: str, assets: dict[str, str] | None = None) -> Path:
    studio = root / "studio"
    (studio / "assets").mkdir(parents=True)
    (studio / "index.html").write_text(index_html, encoding="utf-8")
    for name, text in (assets or {}).items():
        (studio / "assets" / name).write_text(text, encoding="utf-8")
    return studio


def test_check_studio_accepts_a_self_contained_build(tmp_path):
    studio = _write_build(
        tmp_path,
        '<html><head><link rel="stylesheet" href="/static/studio/assets/index-abc.css"></head>'
        '<body><script type="module" src="/static/studio/assets/index-abc.js"></script></body></html>',
        {"index-abc.js": "console.log(1)", "index-abc.css": "body{}"},
    )
    assert check_studio(studio) == []


def test_check_studio_rejects_remote_or_missing_assets(tmp_path):
    studio = _write_build(
        tmp_path,
        '<html><head><link rel="stylesheet" href="https://cdn.example/x.css"></head>'
        '<body><script src="/static/studio/assets/missing.js"></script></body></html>',
        {"chunk.js": 'const s = new WebSocket("wss://elsewhere.example/ws")'},
    )
    errors = check_studio(studio)
    assert any("cdn.example" in error for error in errors)
    assert any("missing asset" in error for error in errors)
    assert any("remote import or WebSocket origin" in error for error in errors)


def test_check_studio_is_silent_when_there_is_no_build(tmp_path):
    assert check_studio(tmp_path / "nowhere") == []


@pytest.mark.skipif(not STUDIO_BUILD.is_file(), reason="Studio not built (npm run build in studio/)")
def test_studio_build_is_served_publicly_under_static(api_client):
    client, main_module = api_client
    main_module.app_state.config.authorization_mode = "enforce"
    response = client.get("/static/studio/")
    assert response.status_code == 200
    assert "<div id=\"root\"></div>" in response.text
    assert check_studio(STUDIO_BUILD.parent) == []
    # the SPA never receives a session by loading; `/auth/me` still requires one
    assert client.get("/auth/me").status_code in (200, 401)
