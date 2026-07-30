"""Phase 6 §11.2: liveness and readiness.

The requirement that shapes these: "background supervisor: a loop that exits
unexpectedly must alert immediately — `/` must not keep reporting green".
So readiness measures loop freshness from the last *completed* iteration, and
a hung loop goes stale rather than looking healthy.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import pytest


def test_liveness_does_not_touch_the_database(api_client, monkeypatch):
    """A liveness probe that fails on a slow query would restart a process
    whose only problem was a slow query."""
    client, main_module = api_client

    def _explode(*args, **kwargs):
        raise AssertionError("liveness must not query the database")

    monkeypatch.setattr(main_module.app_state.db, "schema_is_initialized", _explode)

    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"


def test_readiness_reports_schema_and_writable_state(api_client):
    client, _ = api_client

    response = client.get("/readyz")

    body = response.json()
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["state_path_writable"]["ok"] is True
    assert response.status_code == 200


def test_readiness_fails_closed_when_the_schema_is_incomplete(api_client, monkeypatch):
    """A half-applied migration must report not-ready, not green."""
    client, main_module = api_client
    monkeypatch.setattr(
        main_module.app_state.db, "schema_is_initialized", lambda: False
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["ready"] is False


def test_schema_readiness_requires_the_node_completion_outbox(tmp_path):
    from app.db import Database

    database = Database(str(tmp_path / "schema.db"))
    try:
        assert database.schema_is_initialized() is True
        with database.cursor() as cursor:
            cursor.execute("DROP TABLE execution_completion_operations")
        assert database.schema_is_initialized() is False
    finally:
        database.close()


def test_a_stale_loop_makes_the_process_not_ready(api_client):
    """The whole point: a loop that stopped ticking must not leave the service
    reporting green."""
    client, main_module = api_client
    interval = main_module.app_state.config.scheduler_interval_sec

    import time as time_module

    main_module.app_state._loop_last_tick_monotonic["scheduler"] = (
        time_module.monotonic() - (interval * 10)
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["loops"]["ok"] is False


def test_a_recent_tick_is_ready(api_client):
    client, main_module = api_client
    import time as time_module

    main_module.app_state._loop_last_tick_monotonic["scheduler"] = time_module.monotonic()

    body = client.get("/readyz").json()
    assert body["checks"]["loops"]["ok"] is True


def test_not_being_leader_is_a_serveable_state(api_client):
    """A non-leader serves reads and approvals; refusing readiness for it
    would take a healthy replica out of rotation."""
    client, main_module = api_client
    main_module.app_state._execution_scheduler_is_leader = False

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["checks"]["leader"]["ok"] is True
    assert response.json()["checks"]["leader"]["is_leader"] is False


def test_health_probes_are_authenticated_not_public():
    """Making a probe public would require amending INV-APPROVAL-5's exempt
    set — a protected boundary a health check must not quietly widen."""
    from app.authorization_catalog import PUBLIC_ROUTE_INTERFACES, ROUTE_AUTHORIZATION

    for interface in (("GET", "/healthz"), ("GET", "/readyz")):
        assert interface not in PUBLIC_ROUTE_INTERFACES
        assert interface in ROUTE_AUTHORIZATION


def test_api_role_starts_no_scheduler_or_maintenance_tasks(tmp_path):
    from app.config import AppConfig
    from app.main import AppState

    async def exercise() -> None:
        state = AppState(
            AppConfig(
                servers=[],
                process_role="api",
                db_path=str(tmp_path / "api-only.db"),
                audit_path=str(tmp_path / "api-only-audit.jsonl"),
            )
        )
        state.start_background_tasks()
        assert state._tasks == []
        assert state.expected_loop_intervals() == {}
        await state.stop_background_tasks()

    asyncio.run(exercise())


def test_non_leader_cannot_run_any_scheduler_branch_when_ownership_is_enabled(
    api_client,
):
    _, main_module = api_client
    state = main_module.app_state
    state.config.execution_attempt_reconcile_existing = True
    state._execution_scheduler_is_leader = False
    state._execution_scheduler_fencing_epoch = None

    assert state.may_run_scheduler_tick() is False

    state._execution_scheduler_is_leader = True
    state._execution_scheduler_fencing_epoch = 7
    assert state.may_run_scheduler_tick() is True


def test_operational_metrics_cover_control_plane_nodes_capacity_and_backup(
    api_client, tmp_path
):
    client, main_module = api_client
    backup = tmp_path / "backups" / "20260730T010203Z"
    backup.mkdir(parents=True)
    checksum_content = b"example checksum inventory\n"
    (backup / "CHECKSUMS.sha256").write_bytes(checksum_content)
    created_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (backup / "MANIFEST").write_text(
        f"created_at={created_at}\n"
        f"checksums_sha256={hashlib.sha256(checksum_content).hexdigest()}\n",
        encoding="utf-8",
    )
    main_module.app_state.config.backup_root = str(tmp_path / "backups")

    response = client.get("/operations/metrics")

    assert response.status_code == 200
    body = response.json()
    assert body["process"]["role"] == "all"
    assert body["execution"]["queue"]["depth"] == 0
    assert body["nodes"]["total"] == 0
    assert body["capacity"]["database_bytes"] > 0
    assert body["backup"]["configured"] is True
    assert body["backup"]["integrity_metadata_present"] is True
    # The gate owns the number: reporting age does not silently adopt 26h.
    assert body["backup"]["threshold_seconds"] is None
