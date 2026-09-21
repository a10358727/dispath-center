from __future__ import annotations

import uuid

from app import authorization_enforce
from app.authorization import Action
from app.authorization_catalog import ROUTE_AUTHORIZATION
from tests.test_execution_plan_v2_api import (
    _counts,
    _enable_execution_plan_v2,
    _preview_request,
    _seed_execution_context,
)
from tests.test_run_templates_v2 import OPERATOR_ID, _session_for


def _agent_session(database, seed: dict):
    conversation = database.get_or_create_project_conversation(seed["project_name"])
    approval_id = database.insert_approval(
        "agent_session_open", {}, requester_actor_id=OPERATOR_ID
    )
    return database.apply_agent_session_open_decision(
        approval_id=approval_id,
        project_id=seed["project_id"],
        conversation_id=conversation.id,
        provider_id="claude-agent-sdk",
        workspace_branch=f"agent-session-{uuid.uuid4()}",
        base_version_id=seed["version"]["id"],
        max_turns=200,
        turn_timeout_sec=600,
        decision_actor_id=OPERATOR_ID,
        decision_actor_kind="human",
        decision_mechanism="session",
    )


def test_agent_session_run_preview_is_read_only_and_uses_session_project(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    session = _agent_session(main_module.app_state.db, seed)
    _session_for(client, main_module, OPERATOR_ID)
    before = _counts(main_module.app_state.db)

    response = client.post(
        f"/api/v2/agent-sessions/{session.id}/run-previews",
        json=_preview_request(seed),
    )

    assert response.status_code == 200, response.json()
    assert response.json()["ready"] is True
    assert response.json()["plan"]["project_id"] == seed["project_id"]
    assert _counts(main_module.app_state.db) == before
    assert response.headers["cache-control"] == "no-store"


def test_agent_session_run_request_creates_only_existing_pending_plan_and_replays(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    session = _agent_session(main_module.app_state.db, seed)
    _session_for(client, main_module, OPERATOR_ID)
    body = _preview_request(seed)
    preview = client.post(
        f"/api/v2/agent-sessions/{session.id}/run-previews",
        json=body,
    ).json()
    submit_body = {**body, "expected_plan_digest": preview["plan_digest"]}
    route = f"/api/v2/agent-sessions/{session.id}/run-requests"

    first = client.post(route, headers={"Idempotency-Key": "agent-run-1"}, json=submit_body)
    second = client.post(route, headers={"Idempotency-Key": "agent-run-1"}, json=submit_body)

    assert first.status_code == second.status_code == 202
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert second.json()["execution_plan_id"] == first.json()["execution_plan_id"]
    approval = main_module.app_state.db.get_approval(first.json()["approval_id"])
    assert approval is not None
    assert approval.kind == "execution_plan_v2"
    assert approval.status == "pending"
    assert approval.requester_actor_id == OPERATOR_ID
    with main_module.app_state.db.cursor() as cursor:
        assert cursor.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_agent_session_run_requires_existing_session_and_promoted_project_version(api_client):
    client, main_module = api_client
    _enable_execution_plan_v2(main_module)
    seed = _seed_execution_context(main_module)
    session = _agent_session(main_module.app_state.db, seed)
    _session_for(client, main_module, OPERATOR_ID)
    body = _preview_request(seed)
    before = _counts(main_module.app_state.db)

    missing_session = client.post(
        f"/api/v2/agent-sessions/{uuid.uuid4()}/run-previews",
        json=body,
    )
    missing_version = client.post(
        f"/api/v2/agent-sessions/{session.id}/run-previews",
        json={key: value for key, value in body.items() if key != "project_version_id"},
    )

    assert missing_session.status_code == 404
    assert missing_version.status_code == 422
    assert missing_version.json()["error"]["code"] == "invalid_execution_plan_request"
    assert _counts(main_module.app_state.db) == before


def test_agent_session_run_routes_are_project_operate_and_opaque() -> None:
    routes = {
        ("POST", "/api/v2/agent-sessions/{session_id}/run-previews"),
        ("POST", "/api/v2/agent-sessions/{session_id}/run-requests"),
    }
    for route in routes:
        assert ROUTE_AUTHORIZATION[route].action is Action.PROJECT_OPERATE
        assert ROUTE_AUTHORIZATION[route].resource_kind == "agent_session"
    assert routes.issubset(authorization_enforce._OPAQUE_PROJECT_READ_INTERFACES)
