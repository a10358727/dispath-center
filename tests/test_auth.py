"""Compatible HTTP authentication and immutable request-context integration."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.authentication import LEGACY_ADMIN_ACTOR_ID
from app.authentication import LegacyActorCollisionError
from app.db import Database
from app.audit import read_audit
from app.identity import ActorType, generate_service_token, generate_session_token


@pytest.fixture
def auth_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


@pytest.fixture
def anonymous_shadow_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "shadow.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "shadow-audit.jsonl"))
    monkeypatch.setenv("AUTHORIZATION_MODE", "shadow")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)

    import app.main as main_module

    with TestClient(main_module.app) as client:
        yield client, main_module


def test_no_token_configured_allows_all_requests(api_client):
    client, _main = api_client
    # AUTH_TOKEN 未設定（api_client fixture 用 monkeypatch.delenv 確保），
    # 不帶任何 header 也能存取一般 API。
    assert client.get("/servers").status_code == 200
    assert client.get("/jobs").status_code == 200


def test_no_token_configured_auth_me_is_anonymous_development_context(api_client):
    client, _main = api_client
    response = client.get("/auth/me")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "authenticated": False,
        "authentication_method": "anonymous",
        "oidc_enabled": False,
        "actor": None,
        "project_memberships": [],
        "service_scopes": [],
    }


def test_shadow_audit_is_deferred_until_after_existing_audit_response(
    anonymous_shadow_client,
):
    client, main_module = anonymous_shadow_client

    response = client.get("/events")

    # Mode off on the same fresh state returns this exact empty collection.
    # The denial is appended only after the response has been materialized.
    assert response.status_code == 200
    assert response.json() == []
    records = read_audit(main_module.app_state.config.audit_path)
    assert len(records) == 1
    assert records[0]["action"] == "authorization_shadow_denied"
    assert records[0]["result"] == "would_deny"
    assert records[0]["params"] == {
        "action": "audit.view",
        "resource": "audit",
        "project": None,
        "route": "GET /events",
        "tool": None,
        "reason": "denied_anonymous",
        "principal_kind": "anonymous",
        "shadow_mode": True,
    }
    assert "actor" not in records[0]


def test_shadow_denials_never_block_mutation_approval_or_queue(
    anonymous_shadow_client,
):
    client, main_module = anonymous_shadow_client

    created = client.post(
        "/projects",
        json={"name": "shadow-project", "repo_or_path": "/srv/shadow-project"},
    )
    assert created.status_code == 200
    assert main_module.app_state.db.get_project("shadow-project") is not None

    requested = client.post(
        "/dispatch",
        json={
            "command": "echo shadow-compatible",
            "project": "shadow-project",
            "source": "api",
        },
    )
    assert requested.status_code == 200
    approval_id = requested.json()["id"]
    assert main_module.app_state.db.get_approval(approval_id).status == "pending"

    decided = client.post(f"/approve/{approval_id}")
    assert decided.status_code == 200
    approval = main_module.app_state.db.get_approval(approval_id)
    assert approval.status == "approved"
    assert len(main_module.app_state.db.list_jobs()) == 1

    shadow_routes = [
        record["params"]["route"]
        for record in read_audit(main_module.app_state.config.audit_path)
        if record["action"] == "authorization_shadow_denied"
    ]
    assert "POST /projects" in shadow_routes
    assert "POST /dispatch" in shadow_routes
    assert "POST /approve/{approval_id}" in shadow_routes


def test_index_exempt_from_auth(auth_client):
    client, _main = auth_client
    resp = client.get("/")
    assert resp.status_code == 200


def test_missing_token_is_401(auth_client):
    client, _main = auth_client
    resp = client.get("/servers")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}


def test_auth_me_is_not_an_authentication_exemption(auth_client):
    client, _main = auth_client
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    assert resp.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}


def test_wrong_token_is_401(auth_client):
    client, _main = auth_client
    resp = client.get("/servers", headers={"X-Auth-Token": "wrong"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}


def test_correct_token_is_ok(auth_client):
    client, _main = auth_client
    resp = client.get("/servers", headers={"X-Auth-Token": "secret-token"})
    assert resp.status_code == 200


def test_correct_shared_token_maps_to_durable_safe_legacy_admin(auth_client):
    client, main_module = auth_client
    headers = {"X-Auth-Token": "secret-token"}

    first = client.get("/auth/me", headers=headers)
    second = client.get("/auth/me", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    body = first.json()
    assert body["authenticated"] is True
    assert body["authentication_method"] == "legacy_shared_token"
    assert body["actor"] == {
        "id": LEGACY_ADMIN_ACTOR_ID,
        "type": "legacy",
        "display_name": "legacy-admin",
        "platform_admin": True,
    }
    assert body["project_memberships"] == []
    assert body["service_scopes"] == []
    assert main_module.app_state.db.get_actor(LEGACY_ADMIN_ACTOR_ID) is not None
    assert "secret-token" not in first.text


def test_authenticated_request_audit_has_safe_actor_not_source_or_credential(auth_client):
    client, main_module = auth_client
    resp = client.post(
        "/projects",
        json={"name": "audit-project", "repo_or_path": "/srv/audit-project"},
        headers={"X-Auth-Token": "secret-token"},
    )

    assert resp.status_code == 200
    record = next(
        event
        for event in main_module.app_state.db.list_durable_audit_events(limit=100)
        if event["action"] == "project_created"
    )
    assert record["action"] == "project_created"
    assert record["actor"] == {
        "id": LEGACY_ADMIN_ACTOR_ID,
        "kind": "legacy",
        "authentication": "legacy_shared_token",
    }
    serialized = str(record)
    assert "secret-token" not in serialized
    assert "X-Auth-Token" not in serialized


def test_valid_session_cookie_authenticates_without_legacy_header(auth_client):
    client, main_module = auth_client
    actor = main_module.app_state.db.insert_actor(
        actor_type=ActorType.HUMAN,
        display_name="Session User",
        email="private@example.test",
    )
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)

    resp = client.get("/auth/me")

    assert resp.status_code == 200
    assert resp.json()["actor"] == {
        "id": actor.id,
        "type": "human",
        "display_name": "Session User",
        "platform_admin": False,
    }
    assert issued.raw_token not in resp.text
    assert issued.secret_hash not in resp.text
    assert "private@example.test" not in resp.text


def test_invalid_bearer_does_not_mask_valid_legacy_fallback(auth_client):
    client, _main = auth_client
    resp = client.get(
        "/auth/me",
        headers={
            "Authorization": "Bearer malformed",
            "X-Auth-Token": "secret-token",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["actor"]["id"] == LEGACY_ADMIN_ACTOR_ID


def test_service_bearer_is_rejected_by_default_without_legacy_fallback(auth_client):
    client, main_module = auth_client
    actor = main_module.app_state.db.insert_actor(
        actor_type=ActorType.SERVICE,
        display_name="Disabled service transport",
    )
    main_module.app_state.db.insert_service_account(
        actor_id=actor.id,
        name="disabled-service-transport",
    )
    issued = generate_service_token()
    main_module.app_state.db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=actor.id,
        secret_hash=issued.secret_hash,
        scopes=["project.view"],
        expires_at="2099-01-01T00:00:00+00:00",
    )

    resp = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {issued.raw_token}"},
    )

    assert resp.status_code == 401
    assert resp.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}
    assert issued.raw_token not in resp.text


def test_service_bearer_is_opt_in_and_returns_only_safe_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "legacy-fallback")
    monkeypatch.setenv("SERVICE_TOKEN_AUTH_ENABLED", "true")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        actor = main_module.app_state.db.insert_actor(
            actor_type=ActorType.SERVICE,
            display_name="MCP service",
        )
        main_module.app_state.db.insert_service_account(
            actor_id=actor.id,
            name="mcp-service",
        )
        issued = generate_service_token()
        main_module.app_state.db.insert_service_account_token(
            token_id=issued.id,
            service_account_actor_id=actor.id,
            secret_hash=issued.secret_hash,
            scopes=["project.view", "project.operate"],
            expires_at="2099-01-01T00:00:00+00:00",
        )

        resp = client.get(
            "/auth/me",
            headers={"Authorization": f"Bearer {issued.raw_token}"},
        )

    assert resp.status_code == 200
    assert resp.json()["authentication_method"] == "service_token"
    assert resp.json()["actor"]["id"] == actor.id
    assert resp.json()["service_scopes"] == ["project.operate", "project.view"]
    assert issued.raw_token not in resp.text
    assert issued.secret_hash not in resp.text


def test_disabling_legacy_transport_rejects_shared_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("LEGACY_SHARED_TOKEN_ENABLED", "false")

    import app.main as main_module

    with TestClient(main_module.app) as client:
        resp = client.get(
            "/auth/me", headers={"X-Auth-Token": "secret-token"}
        )

    assert resp.status_code == 401
    assert resp.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}


def test_reserved_legacy_actor_collision_stops_startup_without_rewrite(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "test.db"
    database = Database(str(db_path))
    collision = database.insert_actor(
        actor_id=LEGACY_ADMIN_ACTOR_ID,
        actor_type=ActorType.HUMAN,
        display_name="Existing human",
    )
    database.close()
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("AUTH_TOKEN", "secret-token")
    monkeypatch.setenv("LEGACY_SHARED_TOKEN_ENABLED", "true")

    import app.main as main_module

    with pytest.raises(LegacyActorCollisionError):
        with TestClient(main_module.app):
            pass

    reopened = Database(str(db_path))
    try:
        assert reopened.get_actor(LEGACY_ADMIN_ACTOR_ID) == collision
    finally:
        reopened.close()


def test_correct_token_allows_dispatch(auth_client):
    client, _main = auth_client
    resp = client.post(
        "/dispatch",
        json={"command": "sleep 60"},
        headers={"X-Auth-Token": "secret-token"},
    )
    assert resp.status_code == 200
