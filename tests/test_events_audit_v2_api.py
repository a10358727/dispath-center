"""HTTP-surface tests for the `/api/v2/events`/`/api/v2/audit` thin wrappers
(DG-UI-UNIFICATION v1, U8).

Both wrap the exact legacy `/events`/`/audit` handlers in `app/main.py`
byte-for-byte (same `_audit_records_for_response()`/`audit_coverage()`
engine, same `Action.AUDIT_VIEW`/`"audit"` authorization classification --
see `app/authorization_catalog.py`). Model on
`tests/test_infrastructure_v2_api.py`'s parity-with-legacy pattern.
"""

from __future__ import annotations


def _enable_v2(main_module) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = True


def test_events_and_audit_v2_are_hidden_while_api_v2_is_disabled(api_client):
    client, _main = api_client

    assert client.get("/api/v2/events").status_code == 404
    assert client.get("/api/v2/audit").status_code == 404


def test_events_v2_matches_legacy_events_byte_for_byte(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    client.post("/dispatch", json={"command": "sleep 1"})

    legacy = client.get("/events").json()
    v2 = client.get("/api/v2/events").json()

    assert v2 == legacy
    assert len(v2) >= 1


def test_audit_v2_matches_legacy_audit_byte_for_byte(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    client.post("/dispatch", json={"command": "sleep 1"})

    legacy = client.get("/audit").json()
    v2 = client.get("/api/v2/audit").json()

    assert v2 == legacy
    assert v2 == client.get("/api/v2/events").json()


def test_events_v2_carries_the_same_audit_coverage_header_and_no_store(api_client):
    client, main_module = api_client
    _enable_v2(main_module)

    legacy = client.get("/events")
    v2 = client.get("/api/v2/events")

    assert v2.status_code == 200
    assert v2.headers["X-Audit-Coverage"] == legacy.headers["X-Audit-Coverage"]
    assert v2.headers["Cache-Control"] == "no-store"
    assert v2.headers["Pragma"] == "no-cache"


def test_events_v2_rejects_invalid_pagination_like_legacy(api_client):
    client, main_module = api_client
    _enable_v2(main_module)

    resp = client.get("/api/v2/events", params={"limit": 0})
    assert resp.status_code == 400
    resp = client.get("/api/v2/audit", params={"after_id": -1})
    assert resp.status_code == 400
