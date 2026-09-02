"""Slice 6 identity-administration API compatibility and redaction tests."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.identity import ActorType, RequestContext, generate_service_token


FUTURE = "2099-01-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/identity/service-accounts", None),
        (
            "post",
            "/identity/service-accounts/request",
            {"name": "automation", "description": "CI"},
        ),
        (
            "post",
            "/identity/service-accounts/service-actor/tokens/request",
            {"label": "deploy", "scopes": ["project.view"], "expires_at": FUTURE},
        ),
        (
            "post",
            "/identity/service-tokens/token-id/revoke-request",
            None,
        ),
        ("get", "/projects/example/memberships", None),
        (
            "post",
            "/projects/example/memberships/request",
            {"actor_id": "actor-id", "role": "viewer"},
        ),
        (
            "post",
            "/projects/example/memberships/actor-id/remove-request",
            None,
        ),
    ],
)
def test_identity_administration_routes_are_hidden_by_default(
    api_client, method, path, body
):
    client, _ = api_client

    response = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "identity administration is disabled"}


def test_service_account_list_is_explicit_and_secret_free(api_client):
    client, main_module = api_client
    main_module.app_state.config.identity_admin_enabled = True
    db = main_module.app_state.db

    actor = db.insert_actor(
        actor_type=ActorType.SERVICE,
        display_name="release-bot",
        email="must-not-be-listed@example.test",
    )
    account = db.insert_service_account(
        actor_id=actor.id,
        name="release-bot",
        description="release automation",
    )
    issued = generate_service_token()
    token = db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=actor.id,
        secret_hash=issued.secret_hash,
        scopes=["project.view"],
        expires_at=FUTURE,
        label="read-only",
    )

    response = client.get("/identity/service-accounts")

    assert response.status_code == 200
    assert response.json() == [
        {
            "actor_id": actor.id,
            "name": account.name,
            "description": "release automation",
            "created_by_actor_id": None,
            "created_at": account.created_at,
            "actor": {
                "id": actor.id,
                "type": "service",
                "display_name": "release-bot",
                "platform_admin": False,
                "disabled_at": None,
                "created_at": actor.created_at,
                "updated_at": actor.updated_at,
            },
            "tokens": [
                {
                    "id": token.id,
                    "service_account_actor_id": actor.id,
                    "label": "read-only",
                    "scopes": ["project.view"],
                    "created_by_actor_id": None,
                    "created_at": token.created_at,
                    "expires_at": FUTURE,
                    "last_used_at": None,
                    "revoked_at": None,
                }
            ],
        }
    ]
    encoded = json.dumps(response.json())
    assert "secret_hash" not in encoded
    assert "raw_token" not in encoded
    assert issued.raw_token not in encoded
    assert issued.secret_hash not in encoded
    assert "must-not-be-listed@example.test" not in encoded


@pytest.mark.usefixtures("legacy_posture")
def test_identity_request_routes_store_only_canonical_non_secret_payloads(api_client):
    client, main_module = api_client
    main_module.app_state.config.identity_admin_enabled = True
    db = main_module.app_state.db

    service_actor = db.insert_actor(
        actor_type=ActorType.SERVICE, display_name="existing-service"
    )
    db.insert_service_account(actor_id=service_actor.id, name="existing-service")
    issued = generate_service_token()
    db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=service_actor.id,
        secret_hash=issued.secret_hash,
        scopes=["project.view"],
        expires_at=FUTURE,
    )
    human = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Operator")
    project_id = db.insert_project("canonical-project", "/tmp/canonical-project")
    db.upsert_project_membership(
        project=project_id, actor_id=human.id, role="viewer"
    )

    created = client.post(
        "/identity/service-accounts/request",
        json={"name": "  build-bot  ", "description": "  builds  "},
    )
    assert created.status_code == 200
    assert created.json()["kind"] == "service_account_create"
    account_payload = created.json()["payload"]
    assert isinstance(account_payload.pop("actor_id"), str)
    assert account_payload == {
        "name": "build-bot",
        "description": "builds",
    }

    token_request = client.post(
        f"/identity/service-accounts/{service_actor.id}/tokens/request",
        json={
            "label": "  rotation  ",
            "scopes": ["project.view", "project.view", "audit.view"],
            "expires_at": FUTURE,
        },
    )
    assert token_request.status_code == 200
    token_payload = token_request.json()["payload"]
    assert token_payload == {
        "service_account_actor_id": service_actor.id,
        "label": "rotation",
        "scopes": ["audit.view", "project.view"],
        "expires_at": FUTURE,
    }
    assert "token_id" not in token_payload
    assert "raw_token" not in token_payload
    assert "secret_hash" not in token_payload

    revoke = client.post(
        f"/identity/service-tokens/{issued.id}/revoke-request"
    )
    assert revoke.status_code == 200
    assert revoke.json()["payload"] == {"token_id": issued.id}

    membership = client.post(
        f"/projects/{project_id}/memberships/request",
        json={"actor_id": human.id, "role": "admin"},
    )
    assert membership.status_code == 200
    assert membership.json()["payload"] == {
        "project_id": project_id,
        "actor_id": human.id,
        "role": "admin",
    }

    removal = client.post(
        f"/projects/{project_id}/memberships/{human.id}/remove-request"
    )
    assert removal.status_code == 200
    assert removal.json()["payload"] == {
        "project_id": project_id,
        "actor_id": human.id,
    }


def test_token_approval_displays_raw_value_once_and_lists_never_repeat_it(api_client):
    client, main_module = api_client
    main_module.app_state.config.identity_admin_enabled = True

    account_request = client.post(
        "/identity/service-accounts/request",
        json={"name": "one-time-bot", "description": "token test"},
    )
    assert account_request.status_code == 200
    account_approval_id = account_request.json()["id"]
    account_approval = client.post(f"/approve/{account_approval_id}")
    assert account_approval.status_code == 200
    account = account_approval.json()["service_account"]

    token_request = client.post(
        f"/identity/service-accounts/{account['actor_id']}/tokens/request",
        json={
            "label": "first",
            "scopes": ["project.view"],
            "expires_at": FUTURE,
        },
    )
    assert token_request.status_code == 200
    request_body = token_request.json()
    assert "raw_token" not in json.dumps(request_body)
    token_approval_id = request_body["id"]

    approved = client.post(f"/approve/{token_approval_id}")
    assert approved.status_code == 200
    token = approved.json()["service_token"]
    raw_token = token["raw_token"]
    assert raw_token.startswith(f"dcs_{token['id']}.")
    assert "secret_hash" not in json.dumps(approved.json())

    repeated = client.post(f"/approve/{token_approval_id}")
    assert repeated.status_code == 400
    assert raw_token not in repeated.text

    listed = client.get("/identity/service-accounts")
    assert listed.status_code == 200
    assert raw_token not in listed.text
    assert "raw_token" not in listed.text
    assert "secret_hash" not in listed.text

    persisted_payload = main_module.app_state.db.get_approval(token_approval_id).payload
    assert raw_token not in json.dumps(persisted_payload)
    assert "raw_token" not in persisted_payload
    assert "secret_hash" not in persisted_payload


@pytest.mark.usefixtures("legacy_posture")
def test_membership_upsert_and_remove_are_idempotent_through_approval_api(api_client):
    client, main_module = api_client
    main_module.app_state.config.identity_admin_enabled = True
    db = main_module.app_state.db
    project_id = db.insert_project("member-project", "/tmp/member-project")
    actor = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Member")

    for role in ("viewer", "admin"):
        requested = client.post(
            f"/projects/{project_id}/memberships/request",
            json={"actor_id": actor.id, "role": role},
        )
        assert requested.status_code == 200
        approved = client.post(f"/approve/{requested.json()['id']}")
        assert approved.status_code == 200
        assert approved.json()["membership"]["role"] == role

    memberships = db.list_project_memberships(project_id=project_id)
    assert len(memberships) == 1
    assert memberships[0].role.value == "admin"

    listed = client.get(f"/projects/{project_id}/memberships")
    assert listed.status_code == 200
    assert listed.json()[0]["project"] == "member-project"
    assert listed.json()[0]["project_id"] == project_id
    assert listed.json()[0]["actor_id"] == actor.id
    assert listed.json()[0]["role"] == "admin"
    assert "email" not in json.dumps(listed.json())

    removal = client.post(
        f"/projects/member-project/memberships/{actor.id}/remove-request"
    )
    assert removal.status_code == 200
    removed = client.post(f"/approve/{removal.json()['id']}")
    assert removed.status_code == 200
    assert removed.json()["membership_removed"] is True
    assert db.list_project_memberships(project_id=project_id) == []


def test_direct_api_wiring_returns_issue_secret_once_without_transport(db, audit_path, monkeypatch):
    """Exercise endpoint result plumbing without the sandbox-blocked TestClient."""

    import app.main as main_module

    decider = db.insert_actor(
        actor_type=ActorType.HUMAN,
        display_name="Direct API decider",
        platform_admin=True,
    )
    context = RequestContext(actor=decider, authentication_method="session")
    state = SimpleNamespace(
        db=db,
        config=SimpleNamespace(
            identity_admin_enabled=True,
            audit_path=audit_path,
        ),
        ssh_run=None,
        server_configs={},
    )
    request = SimpleNamespace(
        state=SimpleNamespace(request_context=context),
    )
    monkeypatch.setattr(main_module, "app_state", state)

    account_request = asyncio.run(
        main_module.request_service_account_endpoint(
            main_module.ServiceAccountCreateRequest(
                name="direct-api-bot",
                description="safe endpoint wiring",
            ),
            request,
        )
    )
    account_response = asyncio.run(
        main_module.approve_endpoint(account_request["id"], request)
    )
    actor_id = account_response["service_account"]["actor_id"]

    token_request = asyncio.run(
        main_module.request_service_token_endpoint(
            actor_id,
            main_module.ServiceTokenIssueRequest(
                label="direct",
                scopes=[],
                expires_at=FUTURE,
            ),
            request,
        )
    )
    token_response = asyncio.run(
        main_module.approve_endpoint(token_request["id"], request)
    )
    token = token_response["service_token"]

    assert token["raw_token"].startswith(f"dcs_{token['id']}.")
    assert "secret_hash" not in token
    assert token["raw_token"] not in json.dumps(token_request)
    assert token["raw_token"] not in json.dumps(
        main_module._service_account_to_dict(
            db.get_service_account(actor_id),
            db,
        )
    )

    with pytest.raises(HTTPException) as repeated:
        asyncio.run(main_module.approve_endpoint(token_request["id"], request))
    assert repeated.value.status_code == 400
    assert token["raw_token"] not in str(repeated.value.detail)


def test_identity_admin_dependency_has_exact_disabled_response(monkeypatch):
    import app.main as main_module

    monkeypatch.setattr(
        main_module,
        "app_state",
        SimpleNamespace(config=SimpleNamespace(identity_admin_enabled=False)),
    )

    with pytest.raises(HTTPException) as disabled:
        main_module._require_identity_admin_enabled()
    assert disabled.value.status_code == 404
    assert disabled.value.detail == "identity administration is disabled"
