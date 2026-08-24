"""DG-METRICS-CONTRACT v1 (`docs/DG_METRICS_CONTRACT_DECISION.md`, approved
2026-08-24): read-only `GET /jobs/{job_id}/metrics` endpoint.

Same auth conventions as the existing job results endpoints
(`tests/test_job_results_api.py`): flag-gated 404 when
`METRICS_V1_ENABLED` is off (the default), `AUTH_TOKEN` coverage, and a
happy path against the DB-backed collection/entries rows directly (the
endpoint is a pure read of `run_metrics`/`run_metrics_collection`, so no
job-finish hook needs to run for these tests).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.metrics_v1 import MetricsEntry


def _create_job(client):
    approval_id = client.post("/dispatch", json={"command": "sleep 1"}).json()["id"]
    client.post(f"/approve/{approval_id}")
    return client.get("/jobs").json()[0]["id"]


def test_metrics_endpoint_404_when_flag_is_off(api_client):
    client, main_module = api_client
    job_id = _create_job(client)
    assert main_module.app_state.config.metrics_v1_enabled is False

    resp = client.get(f"/jobs/{job_id}/metrics")
    assert resp.status_code == 404


def test_metrics_endpoint_404_for_unknown_job_when_flag_is_on(api_client):
    client, main_module = api_client
    main_module.app_state.config.metrics_v1_enabled = True

    resp = client.get("/jobs/9999/metrics")
    assert resp.status_code == 404


def test_metrics_endpoint_reports_unknown_when_no_collection_row(api_client):
    client, main_module = api_client
    main_module.app_state.config.metrics_v1_enabled = True
    job_id = _create_job(client)

    resp = client.get(f"/jobs/{job_id}/metrics")
    assert resp.status_code == 200
    assert resp.json() == {
        "job_id": job_id,
        "collection_status": "unknown",
        "reason": None,
        "source_sha256": None,
        "collected_at": None,
        "metrics": [],
    }


def test_metrics_endpoint_happy_path_returns_collected_entries(api_client):
    client, main_module = api_client
    main_module.app_state.config.metrics_v1_enabled = True
    job_id = _create_job(client)
    main_module.app_state.db.replace_run_metrics(
        job_id,
        (
            MetricsEntry(key="loss", value_type="decimal", value_text="0.5"),
            MetricsEntry(key="epoch", value_type="int", value_text="3"),
        ),
        status="collected",
        reason=None,
        source_sha256="a" * 64,
    )

    resp = client.get(f"/jobs/{job_id}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["collection_status"] == "collected"
    assert body["reason"] is None
    assert body["source_sha256"] == "a" * 64
    assert body["collected_at"] is not None
    entries = {m["key"]: m for m in body["metrics"]}
    assert entries["loss"] == {
        "key": "loss",
        "value_type": "decimal",
        "value_text": "0.5",
        "recorded_at": entries["loss"]["recorded_at"],
    }
    assert entries["epoch"]["value_type"] == "int"


def test_metrics_endpoint_reports_invalid_status_with_reason(api_client):
    client, main_module = api_client
    main_module.app_state.config.metrics_v1_enabled = True
    job_id = _create_job(client)
    main_module.app_state.db.replace_run_metrics(
        job_id,
        (),
        status="invalid",
        reason="float_value_rejected",
        source_sha256="b" * 64,
    )

    resp = client.get(f"/jobs/{job_id}/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["collection_status"] == "invalid"
    assert body["reason"] == "float_value_rejected"
    assert body["metrics"] == []


def test_metrics_endpoint_requires_auth_token_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("METRICS_V1_ENABLED", "true")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        assert client.get("/jobs/1/metrics").status_code == 401
        assert (
            client.get(
                "/jobs/1/metrics", headers={"X-Auth-Token": "secret-token"}
            ).status_code
            == 404
        )
