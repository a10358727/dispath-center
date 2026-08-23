"""PERSONAL_PILOT_PLAN.md §6 T2 / D3: read-only job results list/download.

Covers both layers:

- pure helpers in `app.results` (`list_result_files`, `resolve_result_file`)
  directly, including bounds/truncation and symlink-escape rejection;
- the two HTTP routes end to end via `api_client` (missing dir, 404 unknown
  job, download success, traversal/absolute/URL-encoded rejection, and auth
  required when `AUTH_TOKEN` is set).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.results import ResultPathError, list_result_files, resolve_result_file


# ---------------------------------------------------------------------------
# Pure helper unit tests
# ---------------------------------------------------------------------------


def test_list_result_files_missing_dir_is_not_an_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = list_result_files(str(missing))
    assert result == {"collected": False, "truncated": False, "files": []}


def test_list_result_files_lists_regular_files_with_relative_paths(tmp_path):
    result_dir = tmp_path / "results" / "1"
    (result_dir / "nested").mkdir(parents=True)
    (result_dir / "a.txt").write_text("hello")
    (result_dir / "nested" / "b.txt").write_text("world!!")

    result = list_result_files(str(result_dir))
    assert result["collected"] is True
    assert result["truncated"] is False
    paths = {f["path"]: f for f in result["files"]}
    assert set(paths) == {"a.txt", "nested/b.txt"}
    assert paths["a.txt"]["size"] == 5
    assert paths["nested/b.txt"]["size"] == 7
    assert isinstance(paths["a.txt"]["mtime"], float)


def test_list_result_files_skips_symlinks(tmp_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, result_dir / "link.txt")

    result = list_result_files(str(result_dir))
    assert result["files"] == []


def test_list_result_files_truncates_at_max_entries(tmp_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)
    for i in range(12):
        (result_dir / f"f{i}.txt").write_text("x")

    result = list_result_files(str(result_dir), max_entries=10, max_depth=5)
    assert result["collected"] is True
    assert result["truncated"] is True
    assert len(result["files"]) == 10


def test_list_result_files_bounds_depth(tmp_path):
    result_dir = tmp_path / "results" / "1"
    deep = result_dir
    for i in range(8):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    (deep / "too-deep.txt").write_text("x")

    result = list_result_files(str(result_dir), max_entries=500, max_depth=2)
    assert result["collected"] is True
    assert result["truncated"] is True
    assert result["files"] == []


def test_resolve_result_file_accepts_a_plain_relative_path(tmp_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)
    target = result_dir / "metrics.json"
    target.write_text("{}")

    resolved = resolve_result_file(str(result_dir), "metrics.json")
    assert resolved == os.path.realpath(str(target))


def test_resolve_result_file_accepts_a_nested_relative_path(tmp_path):
    result_dir = tmp_path / "results" / "1"
    (result_dir / "nested").mkdir(parents=True)
    target = result_dir / "nested" / "b.txt"
    target.write_text("x")

    resolved = resolve_result_file(str(result_dir), "nested/b.txt")
    assert resolved == os.path.realpath(str(target))


@pytest.mark.parametrize(
    "requested_path",
    [
        "../outside.txt",
        "nested/../../outside.txt",
        "..",
        "a/./b",
        "a//b",
        "a/",
    ],
)
def test_resolve_result_file_rejects_dot_dot_and_malformed_segments(tmp_path, requested_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("secret")

    with pytest.raises(ResultPathError) as excinfo:
        resolve_result_file(str(result_dir), requested_path)
    assert excinfo.value.status_code == 400


def test_resolve_result_file_rejects_absolute_path(tmp_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)

    with pytest.raises(ResultPathError) as excinfo:
        resolve_result_file(str(result_dir), "/etc/passwd")
    assert excinfo.value.status_code == 400


def test_resolve_result_file_rejects_symlink_escape(tmp_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, result_dir / "escape.txt")

    with pytest.raises(ResultPathError) as excinfo:
        resolve_result_file(str(result_dir), "escape.txt")
    assert excinfo.value.status_code == 400


def test_resolve_result_file_404_for_missing_file(tmp_path):
    result_dir = tmp_path / "results" / "1"
    result_dir.mkdir(parents=True)

    with pytest.raises(ResultPathError) as excinfo:
        resolve_result_file(str(result_dir), "nope.txt")
    assert excinfo.value.status_code == 404


def test_resolve_result_file_rejects_directory_target(tmp_path):
    result_dir = tmp_path / "results" / "1"
    (result_dir / "subdir").mkdir(parents=True)

    with pytest.raises(ResultPathError) as excinfo:
        resolve_result_file(str(result_dir), "subdir")
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# HTTP route integration tests
# ---------------------------------------------------------------------------


def _create_job(client):
    approval_id = client.post("/dispatch", json={"command": "sleep 1"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    return client.get("/jobs").json()[0]["id"]


def _result_dir(main_module, job_id):
    from app.results import local_result_dir

    return local_result_dir(job_id, main_module.app_state.config.local_home_dir)


def test_results_list_404_for_unknown_job(api_client):
    client, _main = api_client
    resp = client.get("/jobs/9999/results")
    assert resp.status_code == 404


def test_results_list_missing_dir_returns_empty_uncollected(api_client):
    client, _main = api_client
    job_id = _create_job(client)

    resp = client.get(f"/jobs/{job_id}/results")
    assert resp.status_code == 200
    assert resp.json() == {"collected": False, "truncated": False, "files": []}


def test_results_list_returns_files_once_collected(api_client):
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "metrics.json"), "w") as fh:
        fh.write('{"loss": 0.1}')

    resp = client.get(f"/jobs/{job_id}/results")
    assert resp.status_code == 200
    body = resp.json()
    assert body["collected"] is True
    assert body["truncated"] is False
    assert [f["path"] for f in body["files"]] == ["metrics.json"]


def test_results_download_404_for_unknown_job(api_client):
    client, _main = api_client
    resp = client.get("/jobs/9999/results/metrics.json")
    assert resp.status_code == 404


def test_results_download_returns_file_bytes(api_client):
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "metrics.json"), "w") as fh:
        fh.write('{"loss": 0.1}')

    resp = client.get(f"/jobs/{job_id}/results/metrics.json")
    assert resp.status_code == 200
    assert resp.content == b'{"loss": 0.1}'


def test_results_download_is_attachment_only_and_never_renders_inline(api_client):
    """Result files are workload output, not trusted site content: a result
    HTML file must download, never render (and run script) in this app's
    origin — forced attachment + octet-stream + nosniff, regardless of the
    file's own type."""
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "report.html"), "w") as fh:
        fh.write("<script>alert(1)</script>")

    resp = client.get(f"/jobs/{job_id}/results/report.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert "report.html" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_results_download_rejects_dot_dot_traversal(api_client):
    # A literal ".." path segment in the request URL is collapsed by the
    # HTTP client itself before the request ever reaches the server (RFC
    # 3986 dot-segment removal, done by httpx here) — so this scenario is
    # exercised end to end via the URL-encoded variant below, and directly
    # (bypassing any client-side normalization) by
    # `test_resolve_result_file_rejects_dot_dot_and_malformed_segments`
    # above, which is the authoritative coverage for literal `..` rejection.
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)

    resp = client.get(f"/jobs/{job_id}/results/good/../../outside.txt")
    # Normalizes client-side to `/jobs/{id}/results/outside.txt`, which is a
    # syntactically safe (but non-existent) request — 404, not 400.
    assert resp.status_code == 404


def test_results_download_rejects_url_encoded_dot_dot_traversal(api_client):
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)

    resp = client.get(f"/jobs/{job_id}/results/%2e%2e/outside.txt")
    assert resp.status_code == 400


def test_results_download_missing_file_is_404(api_client):
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)

    resp = client.get(f"/jobs/{job_id}/results/nope.txt")
    assert resp.status_code == 404


def test_results_download_rejects_symlink_escape(api_client):
    client, main_module = api_client
    job_id = _create_job(client)
    result_dir = _result_dir(main_module, job_id)
    os.makedirs(result_dir, exist_ok=True)
    outside = os.path.join(os.path.dirname(result_dir.rstrip("/")), "outside.txt")
    with open(outside, "w") as fh:
        fh.write("secret")
    os.symlink(outside, os.path.join(result_dir, "escape.txt"))

    resp = client.get(f"/jobs/{job_id}/results/escape.txt")
    assert resp.status_code in (400, 404)


def test_results_endpoints_require_auth_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/jobs/1/results").status_code == 401
        assert client.get("/jobs/1/results/metrics.json").status_code == 401
        assert (
            client.get(
                "/jobs/1/results", headers={"X-Auth-Token": "secret-token"}
            ).status_code
            == 404
        )
