"""WP-3B end-to-end: the plan endpoints, through the real app.

This is where RB-DATASET-001's D-5 ruling actually takes effect. A pure
function that *could* reject a legacy dataset proves nothing; the rejection has
to happen on the request path a user reaches.
"""

from __future__ import annotations

import pytest

from app.audit import read_audit


def test_preview_creates_nothing(api_client):
    """A genuine read: the user can ask what would happen without committing."""
    client, main_module = api_client

    before = _counts(main_module)
    response = client.post(
        "/projects/demo/execution-plans/preview",
        json={"command": "python train.py"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert "target_missing" in body["reason_codes"]
    assert body["plan_digest"] is None
    assert _counts(main_module) == before


def _counts(main_module) -> tuple[int, int]:
    with main_module.app_state.db.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) AS n FROM execution_plans")
        plans = cursor.fetchone()["n"]
        cursor.execute("SELECT COUNT(*) AS n FROM approvals")
        approvals = cursor.fetchone()["n"]
    return plans, approvals


def test_preview_reports_every_missing_prerequisite_at_once(api_client):
    client, _ = api_client

    body = client.post(
        "/projects/demo/execution-plans/preview",
        json={"command": "python train.py"},
    ).json()

    assert {
        "project_version_missing",
        "run_profile_missing",
        "dataset_snapshot_not_published",
        "target_missing",
    } <= set(body["reason_codes"])
    assert set(body["missing"]) >= {"project_version", "run_profile", "target"}


def test_run_request_is_rejected_when_the_plan_is_not_ready(api_client):
    """Rejected at request time, never downgraded into a run that claims a
    reproducibility it does not have."""
    client, main_module = api_client
    before = _counts(main_module)

    response = client.post(
        "/projects/demo/runs/request",
        json={"command": "python train.py"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "plan_not_ready"
    assert "target_missing" in detail["reason_codes"]
    # Nothing persisted: no orphan plan, no approval nobody can act on.
    assert _counts(main_module) == before


def test_a_dangerous_command_never_reaches_a_plan(api_client):
    client, main_module = api_client
    before = _counts(main_module)

    body = client.post(
        "/projects/demo/execution-plans/preview",
        json={"command": "rm -rf /"},
    ).json()

    assert "command_dangerous" in body["reason_codes"]
    assert body["plan_digest"] is None
    assert _counts(main_module) == before


def test_run_view_404s_for_an_unknown_plan(api_client):
    client, _ = api_client
    assert client.get("/runs/does-not-exist").status_code == 404


def test_plan_endpoints_are_never_auth_exempt():
    """INV-APPROVAL-5: the middleware protects by default, so a new endpoint is
    only reachable unauthenticated if someone adds it to the exempt set."""
    from app.authorization_catalog import (
        PUBLIC_ROUTE_INTERFACES,
        ROUTE_AUTHORIZATION,
    )

    for interface in (
        ("POST", "/projects/{name}/execution-plans/preview"),
        ("POST", "/projects/{name}/runs/request"),
        ("GET", "/runs/{plan_id}"),
    ):
        assert interface not in PUBLIC_ROUTE_INTERFACES
        assert interface in ROUTE_AUTHORIZATION


def test_the_run_request_is_a_material_action_and_preview_is_not():
    """Preview is a POST only because it takes a body; it creates nothing, so
    it must not carry an operate action."""
    from app.authorization import Action
    from app.authorization_catalog import ROUTE_AUTHORIZATION

    preview = ROUTE_AUTHORIZATION[("POST", "/projects/{name}/execution-plans/preview")]
    request = ROUTE_AUTHORIZATION[("POST", "/projects/{name}/runs/request")]
    assert preview.action is Action.PROJECT_VIEW
    assert request.action is Action.PROJECT_OPERATE
