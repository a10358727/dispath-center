"""HTTP-surface tests for the `/api/v2/jobs` wrappers (DG-UI-UNIFICATION v1,
U3).

Jobs stay legacy-scope objects: these routes are thin wrappers around the
exact legacy `/jobs`/`/dispatch` surface (see
`dispatch_center/api/routers/jobs_v2.py` module docstring). The list/detail
tests assert byte-identical parity with the legacy endpoints (same job via
both surfaces -> identical dict) since both share one projection
(`app.job_projection.job_to_dict`).
"""

from __future__ import annotations


from app.jobqueue import enqueue_job


def _enable_jobs_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


class FakeCommandResult:
    def __init__(self, stdout: str):
        self.stdout = stdout


class RecordingFakeSSH:
    def __init__(self, tail_text: str = "stopped tail\n"):
        self.calls: list[str] = []
        self.tail_text = tail_text

    async def __call__(self, server_name, command, timeout):
        self.calls.append(command)
        if "tmux kill-session" in command:
            return FakeCommandResult("")
        if "tail -n" in command:
            return FakeCommandResult(self.tail_text)
        return FakeCommandResult("")


def _create_queued_job(client, command: str = "sleep 60") -> int:
    approval_id = client.post("/dispatch", json={"command": command}).json()["id"]
    client.post(f"/approve/{approval_id}")
    return client.get("/jobs").json()[0]["id"]


# ---------------------------------------------------------------------------
# Feature gate
# ---------------------------------------------------------------------------


def test_flag_off_is_a_hidden_interface(api_client):
    client, main_module = api_client
    job_id = _create_queued_job(client)
    assert client.get("/api/v2/jobs").status_code == 404
    assert client.get(f"/api/v2/jobs/{job_id}").status_code == 404
    assert client.post("/api/v2/dispatch-requests", json={"command": "sleep 1"}).status_code == 404

    main_module.app_state.config.api_v2_enabled = True
    # product_rbac_v2 still off -> still hidden.
    assert client.get("/api/v2/jobs").status_code == 404


# ---------------------------------------------------------------------------
# List / detail parity with legacy `/jobs`
# ---------------------------------------------------------------------------


def test_list_and_detail_are_byte_identical_to_legacy(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client, command="sleep 42")

    legacy_list = client.get("/jobs").json()
    v2_list = client.get("/api/v2/jobs").json()
    assert v2_list == legacy_list

    legacy_detail = client.get(f"/jobs/{job_id}").json()
    v2_detail = client.get(f"/api/v2/jobs/{job_id}").json()
    assert v2_detail == legacy_detail
    assert v2_detail["command"] == "sleep 42"


def test_list_supports_status_and_project_filters_like_legacy(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    _create_queued_job(client, command="sleep 1")

    legacy = client.get("/jobs", params={"status": "queued"}).json()
    v2 = client.get("/api/v2/jobs", params={"status": "queued"}).json()
    assert v2 == legacy
    assert len(v2) == 1

    assert client.get("/api/v2/jobs", params={"status": "done"}).json() == []


def test_detail_404_for_unknown_job(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    assert client.get("/api/v2/jobs/9999").status_code == 404


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


def test_log_falls_back_to_stored_log_tail_when_not_running(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)
    main_module.app_state.db.update_job(job_id, log_tail="stored tail\n")

    resp = client.get(f"/api/v2/jobs/{job_id}/log")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "job_id": job_id,
        "status": "queued",
        "live": False,
        "log_tail": "stored tail\n",
    }
    legacy = client.get(f"/jobs/{job_id}/log").json()
    assert legacy == body


def test_log_404_for_unknown_job(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    assert client.get("/api/v2/jobs/9999/log").status_code == 404


def test_log_is_live_when_running(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)
    main_module.app_state.db.update_job(job_id, status="running", server="server-a")
    fake_ssh = RecordingFakeSSH(tail_text="live tail\n")
    main_module.app_state.ssh_run = fake_ssh

    resp = client.get(f"/api/v2/jobs/{job_id}/log")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live"] is True
    assert body["log_tail"] == "live tail\n"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def test_results_list_and_download_round_trip(api_client, tmp_path):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)

    from app.results import local_result_dir

    result_dir = local_result_dir(job_id, main_module.app_state.config.local_home_dir)
    import os

    os.makedirs(result_dir, exist_ok=True)
    with open(os.path.join(result_dir, "metrics.json"), "w") as fh:
        fh.write('{"loss": 0.1}')

    list_resp = client.get(f"/api/v2/jobs/{job_id}/results")
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["collected"] is True
    assert [f["path"] for f in body["files"]] == ["metrics.json"]

    download_resp = client.get(f"/api/v2/jobs/{job_id}/results/metrics.json")
    assert download_resp.status_code == 200
    assert download_resp.content == b'{"loss": 0.1}'
    assert download_resp.headers["content-type"] == "application/octet-stream"
    assert download_resp.headers["content-disposition"].startswith("attachment")
    assert download_resp.headers["x-content-type-options"] == "nosniff"


def test_results_list_404_for_unknown_job(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    assert client.get("/api/v2/jobs/9999/results").status_code == 404


def test_results_download_rejects_traversal(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)

    resp = client.get(f"/api/v2/jobs/{job_id}/results/%2e%2e/outside.txt")
    assert resp.status_code == 400


def test_results_endpoints_require_auth_token_when_configured(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("API_V2_ENABLED", "true")
    monkeypatch.setenv("PRODUCT_RBAC_V2_ENABLED", "true")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/api/v2/jobs/1/results").status_code == 401
        assert (
            client.get(
                "/api/v2/jobs/1/results", headers={"X-Auth-Token": "secret-token"}
            ).status_code
            == 404
        )


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


def test_cancel_queued_job_ok(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)

    resp = client.post(f"/api/v2/jobs/{job_id}/cancel")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert client.get(f"/api/v2/jobs/{job_id}").json()["status"] == "cancelled"


def test_cancel_not_queued_returns_400(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    resp = client.post("/api/v2/jobs/9999/cancel")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "job_not_cancelable"


def test_cancel_engineering_owned_job_returns_409(api_client, tmp_path):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    db = main_module.app_state.db
    job = enqueue_job(
        db,
        command="python -m approved_tool",
        type="adhoc",
        project=None,
        audit_path=main_module.app_state.config.audit_path,
        engineering_task_id="owner-task-1",
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )

    resp = client.post(f"/api/v2/jobs/{job.id}/cancel")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "engineering_task_protected"
    # Never actually cancelled.
    assert db.get_job(job.id).status == "queued"


# ---------------------------------------------------------------------------
# Stop-requests
# ---------------------------------------------------------------------------


def test_stop_request_default_source_leaves_approval_pending(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)
    main_module.app_state.db.update_job(job_id, status="running", server="server-a")

    resp = client.post(f"/api/v2/jobs/{job_id}/stop-requests", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert "auto_approved" not in body
    assert body["status"] == "pending"
    assert body["kind"] == "stop"

    approvals = client.get("/approvals?status=pending&kind=stop").json()
    assert any(a["id"] == body["id"] for a in approvals)


def test_stop_request_web_direct_execute_auto_approves(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)
    main_module.app_state.db.update_job(job_id, status="running", server="server-a")
    main_module.app_state.ssh_run = RecordingFakeSSH(tail_text="stopped by web-direct\n")

    resp = client.post(f"/api/v2/jobs/{job_id}/stop-requests", json={"source": "web"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_approved"] is True
    assert body["job"]["status"] == "running"
    assert body["job"]["log_tail"] == "stopped by web-direct\n"


def test_stop_request_not_running_returns_400(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)

    resp = client.post(f"/api/v2/jobs/{job_id}/stop-requests", json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "job_not_running"


def test_stop_request_404_for_unknown_job(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    resp = client.post("/api/v2/jobs/9999/stop-requests", json={})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Diagnose
# ---------------------------------------------------------------------------


def test_diagnose_503_when_no_llm_available(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)
    main_module.app_state.db.update_job(job_id, status="failed", exit_code=1)

    resp = client.post(f"/api/v2/jobs/{job_id}/diagnose")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "diagnose_unavailable"


def test_diagnose_engineering_owned_job_returns_409(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    db = main_module.app_state.db
    job = enqueue_job(
        db,
        command="python -m approved_tool",
        type="adhoc",
        project=None,
        audit_path=main_module.app_state.config.audit_path,
        engineering_task_id="owner-task-2",
        engineering_task_role="coding",
        engineering_attempt_number=1,
    )
    db.update_job(job.id, status="failed", exit_code=1)

    resp = client.post(f"/api/v2/jobs/{job.id}/diagnose")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "engineering_task_protected"


def test_diagnose_non_failed_job_returns_400(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    job_id = _create_queued_job(client)

    resp = client.post(f"/api/v2/jobs/{job_id}/diagnose")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "job_not_failed"


def test_diagnose_404_for_unknown_job(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)
    assert client.post("/api/v2/jobs/9999/diagnose").status_code == 404


# ---------------------------------------------------------------------------
# dispatch-requests
# ---------------------------------------------------------------------------


def test_dispatch_requests_creates_enqueue_approval(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)

    resp = client.post("/api/v2/dispatch-requests", json={"command": "sleep 5"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "enqueue"
    assert body["payload"]["command"] == "sleep 5"

    legacy_approval = client.get("/approvals").json()
    assert any(a["id"] == body["id"] for a in legacy_approval)


def test_dispatch_requests_dangerous_command_returns_400_and_no_approval(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)

    resp = client.post("/api/v2/dispatch-requests", json={"command": "rm -rf /tmp/x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "dangerous_command"
    assert client.get("/approvals").json() == []
    assert client.get("/api/v2/jobs").json() == []


def test_dispatch_requests_web_direct_execute_matches_legacy_dispatch(api_client):
    client, main_module = api_client
    _enable_jobs_v2(main_module)

    resp = client.post(
        "/api/v2/dispatch-requests", json={"command": "sleep 7", "source": "web"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["auto_approved"] is True
    assert body["job"]["status"] == "queued"
    assert body["job"]["command"] == "sleep 7"
