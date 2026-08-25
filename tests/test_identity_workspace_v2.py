"""PR-03 Product v2 identity, sessions, workspace, and shell contracts."""

from __future__ import annotations

import json
from pathlib import Path

from app.authentication import ensure_legacy_admin_actor
from app.authorization import Action
from app.config import ServerConfig
from app.execution_contract import canonical_json_sha256
from app.identity import (
    ActorType,
    ProjectRoleGrantProvenance,
    ProjectRoleV2,
    generate_service_token,
    generate_session_token,
)
from app.server_config import load_servers_config, write_servers_yaml_atomically


ROOT = Path(__file__).parents[1]
WORKSPACE_HTML = ROOT / "static" / "workspace.html"
WORKSPACE_JS = ROOT / "static" / "workspace.js"
WORKSPACE_FEATURES_JS = ROOT / "static" / "workspace-features.js"
LEGACY_HTML = ROOT / "static" / "index.html"

ACTOR_ID = "20000000-0000-0000-0000-000000000031"
OTHER_ACTOR_ID = "20000000-0000-0000-0000-000000000032"
SERVICE_ID = "20000000-0000-0000-0000-000000000033"
GRANTED_AT = "2026-08-07T00:00:00+00:00"


def _enable_v2(main_module, *, product_rbac: bool = True) -> None:
    config = main_module.app_state.config
    config.api_v2_enabled = True
    config.product_rbac_v2_enabled = product_rbac
    config.authorization_mode = "enforce"


def _insert_binding(database, *, project_id: str, actor_id: str, role: ProjectRoleV2) -> None:
    with database.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO project_role_bindings (
                id, project_id, actor_id, role, grant_provenance, granted_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"binding:{project_id}:{actor_id}:{role.value}",
                project_id,
                actor_id,
                role.value,
                ProjectRoleGrantProvenance.PROJECT_BOOTSTRAP.value,
                GRANTED_AT,
            ),
        )


def _session_for(
    client,
    main_module,
    actor_id: str,
    *,
    expires_at: str = "2099-01-01T00:00:00+00:00",
    oidc_identity_id: str | None = None,
):
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        oidc_identity_id=oidc_identity_id,
        secret_hash=issued.secret_hash,
        expires_at=expires_at,
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)
    return issued


def _create_human(
    database,
    *,
    actor_id: str = ACTOR_ID,
    name: str = "Ada",
    platform_admin: bool = False,
):
    return database.insert_actor(
        actor_id=actor_id,
        actor_type=ActorType.HUMAN,
        display_name=name,
        platform_admin=platform_admin,
    )


def _seed_server(main_module, tmp_path, *, name: str = "legacy-server-x") -> dict:
    payload = {
        "name": name,
        "host": "10.0.0.5",
        "user": "train",
        "key": "/nonexistent/id_test",
        "port": 22,
        "gpu": False,
        "tags": [],
        "project_roots": ["~/projects"],
        "dataset_roots": [],
        "enabled": True,
    }
    write_servers_yaml_atomically(
        main_module.app_state.config.servers_yaml_path, {"servers": [payload]}
    )
    main_module.app_state.server_configs = {
        payload["name"]: ServerConfig(
            name=payload["name"],
            host=payload["host"],
            user=payload["user"],
            key=payload["key"],
            gpu=payload["gpu"],
            tags=list(payload["tags"]),
            project_roots=list(payload["project_roots"]),
            dataset_roots=list(payload["dataset_roots"]),
            enabled=payload["enabled"],
        )
    }
    main_module.app_state.server_states.setdefault(payload["name"], None)
    return payload


def test_v2_identity_routes_are_hidden_by_api_gate_and_root_rolls_back(api_client):
    client, main_module = api_client

    hidden = client.get("/api/v2/me")
    legacy_root = client.get("/")

    assert hidden.status_code == 404
    assert hidden.headers["Cache-Control"] == "no-store"
    assert hidden.headers["Pragma"] == "no-cache"
    assert 'id="approval-fab"' in legacy_root.text
    assert "/static/workspace.js" not in legacy_root.text

    main_module.app_state.config.oidc_enabled = True
    main_module.app_state.config.auth_token = "configured-but-v2-hidden"
    still_hidden = client.get("/api/v2/me")
    assert still_hidden.status_code == 404
    assert still_hidden.headers["X-OIDC-Enabled"] == "true"
    main_module.app_state.config.oidc_enabled = False
    main_module.app_state.config.auth_token = None

    main_module.app_state.config.api_v2_enabled = True
    v2_root = client.get("/")
    anonymous = client.get("/api/v2/me")

    assert v2_root.status_code == 200
    assert 'id="workspace-navigation"' in v2_root.text
    assert (
        "/static/workspace.js?v=20260825-infra-workspace"
        in v2_root.text
    )
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_required"
    assert anonymous.headers["Cache-Control"] == "no-store"
    assert anonymous.headers["X-OIDC-Enabled"] == "false"

    main_module.app_state.config.api_v2_enabled = False
    assert 'id="approval-fab"' in client.get("/").text


def test_me_uses_product_roles_without_exposing_oidc_claims_or_grant_metadata(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    actor = _create_human(database)
    project_id = database.insert_project("alpha", "/private/repository/alpha")
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=actor.id,
        role=ProjectRoleV2.VIEWER,
    )
    identity = database.insert_oidc_identity(
        actor_id=actor.id,
        issuer="https://issuer.secret.example/tenant",
        subject="subject-never-returned",
        email="private@example.test",
    )
    _session_for(client, main_module, actor.id, oidc_identity_id=identity.id)

    response = client.get("/api/v2/me")
    payload = response.json()
    serialized = json.dumps(payload, sort_keys=True)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert payload == {
        "actor": {
            "id": actor.id,
            "type": "human",
            "display_name": "Ada",
            "platform_admin": False,
        },
        "authentication": {"method": "session", "oidc_enabled": False},
        "authorization": {"mode": "enforce", "role_model": "product_rbac_v2"},
        "project_roles": [{"project_id": project_id, "roles": ["viewer"]}],
        "service_scopes": [],
    }
    for forbidden in (
        "issuer.secret.example",
        "subject-never-returned",
        "private@example.test",
        identity.id,
        "grant_provenance",
        "binding_id",
        "group",
    ):
        assert forbidden not in serialized


def test_me_projects_legacy_effective_roles_when_product_rbac_is_disabled(api_client):
    client, main_module = api_client
    _enable_v2(main_module, product_rbac=False)
    database = main_module.app_state.db
    actor = _create_human(database)
    project_id = database.insert_project("legacy-alpha", "/private/legacy-alpha")
    database.upsert_project_membership(
        project="legacy-alpha",
        actor_id=actor.id,
        role="admin",
        created_by_actor_id=actor.id,
    )
    _session_for(client, main_module, actor.id)

    payload = client.get("/api/v2/me").json()

    assert payload["authorization"]["role_model"] == "legacy_membership_adapter"
    assert payload["project_roles"] == [
        {
            "project_id": project_id,
            "roles": ["owner", "operator", "reviewer", "dataset_manager"],
        }
    ]


def test_self_reads_accept_scoped_service_and_permitted_legacy_compatibility(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    project_id = database.insert_project("service-alpha", "/private/service-alpha")
    service_actor = database.insert_actor(
        actor_id=SERVICE_ID,
        actor_type=ActorType.SERVICE,
        display_name="Automation",
    )
    database.insert_service_account(actor_id=service_actor.id, name="automation")
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=service_actor.id,
        role=ProjectRoleV2.OPERATOR,
    )
    issued = generate_service_token()
    database.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=service_actor.id,
        secret_hash=issued.secret_hash,
        scopes=[Action.IDENTITY_SELF_VIEW.value, Action.PROJECT_VIEW.value],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.service_token_auth_enabled = True

    service = client.get(
        "/api/v2/me",
        headers={"Authorization": f"Bearer {issued.raw_token}"},
    )

    assert service.status_code == 200
    assert service.json()["actor"]["type"] == "service"
    assert service.json()["project_roles"] == [
        {"project_id": project_id, "roles": ["operator"]}
    ]
    sessions = client.get(
        "/api/v2/me/sessions",
        headers={"Authorization": f"Bearer {issued.raw_token}"},
    )
    assert sessions.status_code == 200
    assert sessions.json()["items"] == []

    main_module.app_state.config.auth_token = "temporary-rollback-token"
    ensure_legacy_admin_actor(database)
    legacy = client.get(
        "/api/v2/me",
        headers={"X-Auth-Token": "temporary-rollback-token"},
    )
    assert legacy.status_code == 200
    assert legacy.json()["actor"]["type"] == "legacy"
    assert legacy.json()["authentication"]["method"] == "legacy_shared_token"


def test_sessions_are_actor_isolated_paginated_and_never_expose_identifiers(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    actor = _create_human(database)
    outsider = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Outsider")
    identity = database.insert_oidc_identity(
        actor_id=actor.id,
        issuer="https://identity.example.test",
        subject="ada-subject",
    )

    current = _session_for(client, main_module, actor.id, oidc_identity_id=identity.id)
    expired = generate_session_token()
    database.insert_actor_session(
        session_id=expired.id,
        actor_id=actor.id,
        secret_hash=expired.secret_hash,
        expires_at="2020-01-01T00:00:00+00:00",
    )
    revoked = generate_session_token()
    database.insert_actor_session(
        session_id=revoked.id,
        actor_id=actor.id,
        secret_hash=revoked.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    database.revoke_actor_session(revoked.id, revoked_at="2026-08-07T03:00:00+00:00")
    outsider_session = generate_session_token()
    database.insert_actor_session(
        session_id=outsider_session.id,
        actor_id=outsider.id,
        secret_hash=outsider_session.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    with database.cursor() as cursor:
        cursor.execute(
            "UPDATE actor_sessions SET created_at = ? WHERE id = ?",
            ("2026-08-07T03:00:00+00:00", current.id),
        )
        cursor.execute(
            "UPDATE actor_sessions SET created_at = ? WHERE id = ?",
            ("2026-08-07T02:00:00+00:00", expired.id),
        )
        cursor.execute(
            "UPDATE actor_sessions SET created_at = ? WHERE id = ?",
            ("2026-08-07T01:00:00+00:00", revoked.id),
        )

    first = client.get("/api/v2/me/sessions", params={"limit": 2})
    first_payload = first.json()
    second = client.get(
        "/api/v2/me/sessions",
        params={"limit": 2, "cursor": first_payload["next_cursor"]},
    )
    combined = first_payload["items"] + second.json()["items"]
    serialized = json.dumps(
        {"first": first_payload, "second": second.json()}, sort_keys=True
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first_payload["next_cursor"] is not None
    assert second.json()["next_cursor"] is None
    assert [item["state"] for item in combined] == ["active", "expired", "revoked"]
    assert [item["current"] for item in combined] == [True, False, False]
    assert combined[0]["authentication_source"] == "oidc"
    assert first_payload["remote_revocation_supported"] is False
    for forbidden in (
        current.id,
        expired.id,
        revoked.id,
        outsider_session.id,
        current.raw_token,
        expired.raw_token,
        revoked.raw_token,
        identity.id,
        "secret_hash",
        "session_id",
    ):
        assert forbidden not in serialized


def test_workspace_filters_before_limiting_and_returns_only_honest_summaries(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    actor = _create_human(database)
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")
    project_a = database.insert_project("alpha", "/secret/alpha", summary="Visible")
    database.insert_project("beta", "/secret/beta", summary="Hidden")
    _insert_binding(
        database,
        project_id=project_a,
        actor_id=actor.id,
        role=ProjectRoleV2.REVIEWER,
    )
    _session_for(client, main_module, actor.id)

    for index in range(12):
        database.insert_job(
            command=f"private-command-alpha-{index}",
            project="alpha",
            status="queued",
        )
    database.insert_job(
        command="private-command-beta",
        project="beta",
        status="queued",
    )
    visible_approval = database.insert_approval(
        "apply_patch",
        {"project": "alpha", "secret": "visible-payload-secret"},
        requester_actor_id=requester.id,
    )
    self_approval = database.insert_approval(
        "apply_patch",
        {"project": "alpha", "secret": "self-payload-secret"},
        requester_actor_id=actor.id,
    )
    hidden_approval = database.insert_approval(
        "apply_patch",
        {"project": "beta", "secret": "hidden-payload-secret"},
        requester_actor_id=requester.id,
    )

    response = client.get("/api/v2/workspace")
    payload = response.json()
    serialized = json.dumps(payload, sort_keys=True)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [project["id"] for project in payload["projects"]] == [project_a]
    assert len(payload["recent_runs"]) == 10
    assert all(run["project_id"] == project_a for run in payload["recent_runs"])
    assert all(run["source"] == "legacy_job" for run in payload["recent_runs"])
    assert [run["id"] for run in payload["recent_runs"]] == sorted(
        (run["id"] for run in payload["recent_runs"]), reverse=True
    )
    approvals = {approval["id"]: approval for approval in payload["pending_approvals"]}
    assert set(approvals) == {visible_approval, self_approval}
    assert approvals[visible_approval]["can_decide"] is True
    assert approvals[self_approval]["can_decide"] is False
    assert approvals[self_approval]["decision_reason"] == "denied_high_risk_self_decision"
    assert payload["recent_dataset_assets"] == {
        "items": [],
        "state": "disabled",
    }
    assert payload["capabilities"]["recent_runs"]["state"] == "legacy_job_adapter"
    assert payload["features"]["project_environments_v1"]["value"] is False
    assert payload["capabilities"]["project_environments_v1"] == {
        "implemented": True,
        "enabled": False,
        "state": "disabled",
    }
    assert payload["capabilities"]["dataset_assets"] == {
        "implemented": True,
        "enabled": False,
        "state": "disabled",
    }
    assert "dataset_sharing_v2" not in payload["features"]
    assert "dataset_sharing" not in payload["capabilities"]
    for forbidden in (
        "private-command",
        "payload-secret",
        "/secret/alpha",
        "/secret/beta",
    ):
        assert forbidden not in serialized
    assert hidden_approval not in {
        approval["id"] for approval in payload["pending_approvals"]
    }


def test_workspace_platform_admin_sees_all_projects_without_role_bindings(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    admin = _create_human(database, platform_admin=True)
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")
    project_ids = {
        database.insert_project("alpha", "/private/admin-alpha"),
        database.insert_project("beta", "/private/admin-beta"),
    }
    for project_name in ("alpha", "beta"):
        database.insert_job(
            command=f"private-platform-command-{project_name}",
            project=project_name,
            status="queued",
        )
    other_approval = database.insert_approval(
        "apply_patch",
        {"project": "alpha", "secret": "other-approval-secret"},
        requester_actor_id=requester.id,
    )
    own_approval = database.insert_approval(
        "apply_patch",
        {"project": "beta", "secret": "own-approval-secret"},
        requester_actor_id=admin.id,
    )
    _session_for(client, main_module, admin.id)

    response = client.get("/api/v2/workspace")
    payload = response.json()
    serialized = json.dumps(payload, sort_keys=True)

    assert response.status_code == 200
    assert {project["id"] for project in payload["projects"]} == project_ids
    assert all(project["roles"] == [] for project in payload["projects"])
    assert {run["project_id"] for run in payload["recent_runs"]} == project_ids
    approvals = {item["id"]: item for item in payload["pending_approvals"]}
    assert approvals[other_approval]["can_decide"] is True
    assert approvals[other_approval]["decision_reason"] == "allowed_platform_admin"
    assert approvals[own_approval]["can_decide"] is False
    assert approvals[own_approval]["decision_reason"] == (
        "denied_high_risk_self_decision"
    )
    for forbidden in (
        "/private/admin-alpha",
        "/private/admin-beta",
        "private-platform-command",
        "approval-secret",
    ):
        assert forbidden not in serialized


def test_workspace_dataset_publish_capability_reports_only_local_path_availability(
    api_client,
):
    client, main_module = api_client
    _enable_v2(main_module)
    config = main_module.app_state.config
    config.dataset_publish_v2_enabled = True
    actor = _create_human(main_module.app_state.db)
    _session_for(client, main_module, actor.id)

    config.dataset_publish_local_roots = ()
    without_roots = client.get("/api/v2/workspace")

    assert without_roots.status_code == 200
    assert without_roots.json()["capabilities"]["dataset_publish"] == {
        "implemented": True,
        "enabled": True,
        "state": "available",
        "local_path_enabled": False,
    }

    secret_root = "/private/dataset-publish-root"
    config.dataset_publish_local_roots = (secret_root,)
    with_roots = client.get("/api/v2/workspace")
    serialized = json.dumps(with_roots.json(), sort_keys=True)

    assert with_roots.status_code == 200
    assert with_roots.json()["capabilities"]["dataset_publish"] == {
        "implemented": True,
        "enabled": True,
        "state": "available",
        "local_path_enabled": True,
    }
    assert secret_root not in serialized


def test_workspace_decides_legacy_enqueue_without_leaving_product_ui(api_client):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    admin = _create_human(database, platform_admin=True)
    project_id = database.insert_project("alpha", "/private/alpha")
    payload = {
        "command": "printf unified-workspace",
        "type": "adhoc",
        "project": "alpha",
        "require_tag": None,
        "pin_server": None,
        "depends_on": [],
        "gpus_needed": None,
        "priority": "normal",
    }
    approval_id = database.insert_approval(
        "enqueue",
        payload,
        requester_actor_id=admin.id,
    )
    _session_for(client, main_module, admin.id)

    workspace = client.get("/api/v2/workspace")
    detail = client.get(f"/api/v2/approvals/{approval_id}")

    assert workspace.status_code == 200
    summary = next(
        item
        for item in workspace.json()["pending_approvals"]
        if item["id"] == approval_id
    )
    assert summary["project_id"] == project_id
    assert summary["can_decide"] is True
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    assert detail.json() == {
        "id": approval_id,
        "kind": "enqueue",
        "status": "pending",
        "created_at": detail.json()["created_at"],
        "decided_at": None,
        "requester_actor_id": admin.id,
        "requester_is_self": True,
        "can_decide": True,
        "decision_reason": "allowed_platform_admin",
        "payload": payload,
        "payload_digest": canonical_json_sha256(payload),
        "payload_contract_version": None,
        "payload_verified": False,
        "review_mode": "compatibility_snapshot",
        "review": {
            "effect": "enqueue_job",
            "snapshot_digest_rechecked_at_decision": True,
            "legacy_unpinned": True,
        },
    }

    decision_url = f"/api/v2/approvals/{approval_id}/decisions"
    body = {"decision": "approve", "note": "reviewed in unified Workspace"}
    missing_digest = client.post(
        decision_url,
        json=body,
        headers={"Idempotency-Key": "unified-enqueue-missing-digest"},
    )
    changed_digest = client.post(
        decision_url,
        json=body,
        headers={
            "Idempotency-Key": "unified-enqueue-stale-digest",
            "X-Approval-Payload-Digest": "0" * 64,
        },
    )
    headers = {
        "Idempotency-Key": "unified-enqueue-decision",
        "X-Approval-Payload-Digest": detail.json()["payload_digest"],
    }
    decided = client.post(decision_url, json=body, headers=headers)
    replayed = client.post(decision_url, json=body, headers=headers)

    assert missing_digest.status_code == 400
    assert missing_digest.json()["error"]["code"] == (
        "approval_payload_digest_required"
    )
    assert changed_digest.status_code == 409
    assert changed_digest.json()["error"]["code"] == "approval_payload_changed"
    assert decided.status_code == 202
    assert decided.json()["compatibility"] is True
    assert decided.json()["replayed"] is False
    assert decided.json()["status"] == "approved"
    job_id = decided.json()["job_id"]
    assert database.get_job(job_id).command == payload["command"]
    assert replayed.status_code == 202
    assert replayed.json() == {
        "approval_id": approval_id,
        "compatibility": True,
        "replayed": True,
        "status": "approved",
    }
    assert len(database.list_jobs()) == 1


def test_workspace_lists_and_reviews_a_legacy_server_update_compatibility_card(
    api_client, tmp_path
):
    """DG-UI-UNIFICATION v1 U1: a legacy `server_update` approval — one of the
    reported dead-end kinds — is now listable and reviewable in the Product
    v2 Workspace as an unpinned compatibility snapshot."""

    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    _seed_server(main_module, tmp_path)
    admin = _create_human(database, platform_admin=True)
    #: `server_update` is high-risk (`HIGH_RISK_APPROVAL_KINDS` = every kind
    #: except enqueue/stop) — the requester must differ from the decider or
    #: the default-off self-approval boundary denies the decision.
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")
    payload = {"name": "legacy-server-x", "updates": {"host": "10.0.0.99"}}
    approval_id = database.insert_approval(
        "server_update", payload, requester_actor_id=requester.id
    )
    _session_for(client, main_module, admin.id)

    listing = client.get("/api/v2/approvals?status=pending")
    detail = client.get(f"/api/v2/approvals/{approval_id}")

    assert listing.status_code == 200
    summary = next(
        item for item in listing.json()["items"] if item["id"] == approval_id
    )
    assert summary["kind"] == "server_update"
    assert summary["can_decide"] is True
    assert summary["project_id"] is None
    assert detail.status_code == 200
    assert detail.headers["Cache-Control"] == "no-store"
    body = detail.json()
    assert body["kind"] == "server_update"
    assert body["payload"] == payload
    assert body["payload_digest"] == canonical_json_sha256(payload)
    assert body["payload_contract_version"] is None
    assert body["payload_verified"] is False
    assert body["review_mode"] == "compatibility_snapshot"
    assert body["review"] == {
        "effect": "legacy_approval_decision",
        "snapshot_digest_rechecked_at_decision": True,
        "legacy_unpinned": True,
    }


def test_workspace_decides_legacy_server_update_through_the_generic_engine(
    api_client, tmp_path
):
    """The generic legacy decision branch materializes identically to legacy
    `POST /approve/{id}` — same `approvals_module.approve()` engine, same
    servers.yaml atomic write."""

    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    _seed_server(main_module, tmp_path)
    admin = _create_human(database, platform_admin=True)
    #: `server_update` is high-risk (`HIGH_RISK_APPROVAL_KINDS` = every kind
    #: except enqueue/stop) — the requester must differ from the decider or
    #: the default-off self-approval boundary denies the decision.
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")
    payload = {"name": "legacy-server-x", "updates": {"host": "10.0.0.99"}}
    approval_id = database.insert_approval(
        "server_update", payload, requester_actor_id=requester.id
    )
    _session_for(client, main_module, admin.id)

    detail = client.get(f"/api/v2/approvals/{approval_id}").json()
    decision_url = f"/api/v2/approvals/{approval_id}/decisions"
    body = {"decision": "approve", "note": "reviewed in unified Workspace"}
    headers = {
        "Idempotency-Key": "unified-server-update-decision",
        "X-Approval-Payload-Digest": detail["payload_digest"],
    }

    decided = client.post(decision_url, json=body, headers=headers)
    replayed = client.post(decision_url, json=body, headers=headers)

    assert decided.status_code == 202
    assert decided.json() == {
        "approval_id": approval_id,
        "compatibility": True,
        "replayed": False,
        "status": "approved",
    }
    assert replayed.json()["replayed"] is True
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    updated = next(s for s in on_disk["servers"] if s["name"] == "legacy-server-x")
    assert updated["host"] == "10.0.0.99"
    assert main_module.app_state.server_configs["legacy-server-x"].host == "10.0.0.99"
    assert database.get_approval(approval_id).status == "approved"


def test_workspace_rejects_a_legacy_compatibility_approval(api_client, tmp_path):
    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    _seed_server(main_module, tmp_path)
    admin = _create_human(database, platform_admin=True)
    #: `server_update` is high-risk (`HIGH_RISK_APPROVAL_KINDS` = every kind
    #: except enqueue/stop) — the requester must differ from the decider or
    #: the default-off self-approval boundary denies the decision.
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")
    payload = {"name": "legacy-server-x", "updates": {"host": "10.0.0.99"}}
    approval_id = database.insert_approval(
        "server_update", payload, requester_actor_id=requester.id
    )
    _session_for(client, main_module, admin.id)

    detail = client.get(f"/api/v2/approvals/{approval_id}").json()
    decision_url = f"/api/v2/approvals/{approval_id}/decisions"
    decided = client.post(
        decision_url,
        json={"decision": "reject", "note": "not needed"},
        headers={
            "Idempotency-Key": "unified-server-update-reject",
            "X-Approval-Payload-Digest": detail["payload_digest"],
        },
    )

    assert decided.status_code == 202
    assert decided.json()["status"] == "rejected"
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["host"] == "10.0.0.5"
    assert database.get_approval(approval_id).status == "rejected"


def test_workspace_refuses_one_time_secret_approve_but_allows_reject(api_client):
    """`ONE_TIME_SECRET_APPROVAL_KINDS` (service_token_issue/node_enroll/
    node_rotate) never get an `approve` path through this generic review
    surface — their response carries a raw secret with nowhere safe to show
    it — but `reject` stays available, mirroring the legacy disabled-button
    semantics."""

    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    admin = _create_human(database, platform_admin=True)
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")
    service_actor = database.insert_actor(
        actor_id="20000000-0000-0000-0000-000000000099",
        actor_type=ActorType.SERVICE,
        display_name="Automation",
    )
    database.insert_service_account(actor_id=service_actor.id, name="automation")
    payload = {
        "service_account_actor_id": service_actor.id,
        "label": "ci",
        "scopes": [Action.PROJECT_VIEW.value],
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    approval_id = database.insert_approval(
        "service_token_issue", payload, requester_actor_id=requester.id
    )
    _session_for(client, main_module, admin.id)
    decision_url = f"/api/v2/approvals/{approval_id}/decisions"
    detail = client.get(f"/api/v2/approvals/{approval_id}").json()
    digest_headers = {"X-Approval-Payload-Digest": detail["payload_digest"]}

    approve_attempt = client.post(
        decision_url,
        json={"decision": "approve", "note": None},
        headers={"Idempotency-Key": "one-time-secret-approve", **digest_headers},
    )

    assert approve_attempt.status_code == 409
    assert approve_attempt.json()["error"]["code"] == (
        "one_time_secret_approval_requires_secure_client"
    )
    assert "一次性秘密" in approve_attempt.json()["error"]["message"]
    assert database.get_approval(approval_id).status == "pending"

    reject_attempt = client.post(
        decision_url,
        json={"decision": "reject", "note": "use secure client instead"},
        headers={"Idempotency-Key": "one-time-secret-reject", **digest_headers},
    )

    assert reject_attempt.status_code == 202
    assert reject_attempt.json()["status"] == "rejected"
    assert database.get_approval(approval_id).status == "rejected"


def test_workspace_frontend_is_v2_only_role_aware_and_never_persists_tokens():
    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    legacy = LEGACY_HTML.read_text(encoding="utf-8")
    combined = "\n".join((html, javascript, legacy))

    assert (
        'href="/static/workspace.css?v=20260825-infra-workspace"'
        in html
    )
    assert (
        'src="/static/workspace-features.js?v=20260825-infra-workspace"'
        in html
    )
    assert (
        'src="/static/workspace.js?v=20260825-infra-workspace"'
        in html
    )
    assert 'data-role-navigation="approval"' in html
    assert 'data-role-navigation="dataset"' in html
    assert 'data-role-navigation="bootstrap"' in html
    assert 'id="bootstrap-form"' in html
    assert 'id="dataset-publish-panel"' in html
    assert 'id="run-create-panel"' in html
    assert 'id="dataset-publish-local-path"' in html
    assert 'const PRODUCT_READ_PATHS = new Set([' in javascript
    for path in ("/api/v2/me", "/api/v2/me/sessions", "/api/v2/workspace"):
        assert path in javascript
    for path in (
        "/api/v2/projects/bootstrap-previews",
        "/api/v2/projects/bootstrap-requests",
        "/api/v2/projects/${projectId}/workspace",
        "/api/v2/approvals/${approvalId}",
        "/api/v2/approvals/${detail.id}/decisions",
        "/api/v2/projects/${projectId}/dataset-publish-previews",
        "/api/v2/projects/${preview.project_id}/dataset-publish-requests",
        "/api/v2/projects/${projectId}/run-previews",
        "/api/v2/projects/${state.runCreateProjectId}/run-requests",
        "/api/v2/dataset-assets/${assetId}?project_id=${encodeURIComponent(projectId)}",
        "/api/v2/runs/${planId}",
        "/api/v2/runs/${planId}/artifacts?limit=50",
        "/api/v2/runs/${detail.plan_id}/clone-previews",
        "/api/v2/runs/${detail.plan_id}/stop-requests",
        "/api/v2/runs/compare?${query.toString()}",
    ):
        assert path in javascript
    assert 'headers["Idempotency-Key"]' in javascript
    assert 'String(asset.access_mode || "owned")' in javascript
    assert "asset.scope_project_id" in javascript
    #: DG-UI-UNIFICATION v1 U2 (docs/DECISIONS.md 2026-08-25): summary card
    #: notes are full Traditional Chinese now (translated from the English
    #: "Scoped owned/shared" pin this replaces).
    assert "授權範圍內的自有／共享" in javascript
    assert "Dataset Assets 尚未實作" not in javascript
    assert "PR-07 pending" not in javascript
    assert 'parameter_schema: []' in javascript
    assert 'argv_template: [' in javascript
    for forbidden_path in (
        'fetch("/projects',
        'fetch("/jobs',
        'fetch("/approvals',
        'fetch("/datasets',
        'fetch("/auth/me',
    ):
        assert forbidden_path not in javascript
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        assert storage not in combined
    assert 'headers["X-Auth-Token"] = state.legacyToken' in javascript
    assert 'id="legacy-token-btn" class="button button-quiet" type="button" hidden' in html
    assert "state.authenticationModeKnown && !state.oidcEnabled" in javascript
    assert 'element("legacy-token-btn").hidden = !legacyOnly || method === "session"' in javascript
    assert "state.authenticationModeKnown = false" in javascript
    assert 'let authToken = "";' in legacy
    assert "伺服器重新授權" in html
    assert "不寫入 browser storage" in html
    assert "DATASET_PUBLISH_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert "state.workspace.capabilities.dataset_publish" in javascript
    assert "const requestSerial = ++state.runCreateWorkspaceSerial;" in javascript
    assert "requestSerial !== state.runCreateWorkspaceSerial" in javascript
    assert "const requestSerial = ++state.runCreateAssetSerial;" in javascript
    assert "requestSerial !== state.runCreateAssetSerial" in javascript
    assert "if (!projectId) return;" in javascript
    assert "const previewSerial = ++state.runCreatePreviewSerial;" in javascript
    assert "previewSerial !== state.runCreatePreviewSerial" in javascript
    assert "++state.runCreatePreviewSerial;" in javascript
    assert 'element("run-create-preview-btn").disabled = true;' in javascript
    assert "state.runCreateAsset.contract.asset_id" in javascript
    assert 'if (event.target === element("run-create-project")) return;' in javascript
    assert 'element("run-create-template").addEventListener("change", () => {' in javascript
    assert 'element("run-create-dataset-selection").addEventListener("change", () => {' in javascript
    assert 'JSON.stringify(preview).includes(body.source.path)' in javascript
    assert "{ idempotency: false }" in javascript
    #: DG-UI-UNIFICATION v1 U2 (docs/DECISIONS.md 2026-08-25): full-Chinese
    #: pass — a sample translated eyebrow and the shared collapsed-raw-JSON
    #: summary text, both used consistently across every panel.
    assert '<span class="eyebrow">總覽</span>' in html
    assert "查看原始內容" in html


def test_workspace_product_run_experience_is_v2_only_and_honest():
    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    run_workflow = javascript[
        javascript.index("function renderRuns()") : javascript.index(
            "function renderApprovals()"
        )
    ]

    for field_id in (
        "run-state",
        "run-detail-panel",
        "run-detail-state",
        "run-clone-btn",
        "run-stop-btn",
        "run-artifacts-btn",
        "run-clone-overrides",
        "run-timeline",
        "run-artifact-list",
        "run-compare-left",
        "run-compare-right",
        "run-compare-btn",
        "run-compare-result",
    ):
        assert f'id="{field_id}"' in html
    for allowlist in (
        "PRODUCT_RUN_DETAIL_PATH",
        "PRODUCT_RUN_ARTIFACT_PATH",
        "PRODUCT_RUN_MUTATION_PATH",
        "PRODUCT_RUN_COMPARE_PATH",
    ):
        assert allowlist in javascript
    assert 'run.source === "execution_plan_product_projection"' in run_workflow
    assert 'run.contract_kind || run.type || "unknown"' in run_workflow
    assert '{ parameter_overrides: overrides }' in run_workflow
    assert '{ idempotency: false }' in run_workflow
    assert 'body: JSON.stringify(body)' in javascript
    #: DG-UI-UNIFICATION v1 U2: artifact-metadata honesty line is Traditional
    #: Chinese now (translated from the English "metadata only" pin).
    assert "僅 metadata" in run_workflow
    assert "Job terminal status 尚未被此 request 改動" in run_workflow
    assert "unknown 不會被補造成差異" in html
    #: DG-UI-UNIFICATION v1 U2: compare-dimension equality is a Traditional
    #: Chinese tri-state ("相同"/"不同"/"未知") — `dimension.availability`
    #: itself stays the raw server enum, only our computed `equal` label
    #: is translated.
    assert '"相同" : "不同"' in run_workflow
    assert 'node("strong", String(artifact.relative_path))' in run_workflow
    assert ".innerHTML" not in run_workflow
    assert "localStorage" not in run_workflow
    assert "sessionStorage" not in run_workflow
    assert "indexedDB" not in run_workflow
    clone_workflow = run_workflow[
        run_workflow.index("async function previewRunClone()") : run_workflow.index(
            "async function requestRunStop()"
        )
    ]
    assert "JSON.stringify(preview" not in clone_workflow
    assert "preview.request" not in clone_workflow
    assert "preview.plan)" not in clone_workflow
    assert "preview.plan," not in clone_workflow
    assert '"execution_plan_v2", "experiment_create_v2", "stop"' in javascript


def test_workspace_jobs_panel_is_v2_only_and_ported_faithfully():
    """DG-UI-UNIFICATION v1 U3: the legacy Jobs/Runtime panel migrated into
    the v2 Workspace as thin `/api/v2/jobs` wrappers -- nav entry, section,
    empty state, reviewed-path allowlist additions, and action-button labels
    all pinned so a future edit cannot silently drop one."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    jobs_workflow = javascript[
        javascript.index("// ---- 工作（DG-UI-UNIFICATION v1 U3）") : javascript.index(
            "function renderApprovals()"
        )
    ]

    assert 'data-section="jobs"' in html
    assert 'id="section-jobs" data-workspace-section="jobs" hidden' in html
    assert 'id="jobs-tbody"' in html
    assert 'id="job-dispatch-form"' in html
    assert 'id="job-dispatch-command"' in html
    assert 'id="job-dispatch-project"' in html
    assert 'id="job-log-panel"' in html
    assert 'id="job-results-list"' in html
    assert 'id="job-results-metrics"' in html
    assert "目前沒有任務" in jobs_workflow

    #: Reviewed-path allowlist additions -- literal job-collection/dispatch-
    #: requests paths plus the two id-scoped regexes.
    assert '"/api/v2/jobs",' in javascript
    assert '"/api/v2/dispatch-requests",' in javascript
    assert "JOBS_READ_PATH" in javascript
    assert "JOBS_MUTATION_PATH" in javascript
    assert "JOBS_READ_PATH.test(parsed.pathname)" in javascript
    assert "JOBS_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert (
        "overview|projects|project-bootstrap|runs|jobs|infrastructure|approvals|datasets|sessions"
        in javascript
    )

    #: Section-activation load, not a global poll timer.
    assert 'if (section === "jobs" && state.me) loadJobs();' in javascript
    assert "setInterval" not in jobs_workflow

    #: Per-status action visibility ported from the legacy `renderJobs()`.
    assert "window.WorkspaceUI.jobRowActions(job)" in jobs_workflow
    assert "查看日誌" in jobs_workflow
    assert "取消" in jobs_workflow
    assert "停止（可能立即執行）" in jobs_workflow
    assert "診斷" in jobs_workflow
    assert "查看 AI 工程任務（尚未實作連結）" in jobs_workflow
    assert "function jobRowActions(job)" in features
    assert "function jobElapsed(job)" in features
    assert ".innerHTML" not in jobs_workflow
    assert "localStorage" not in jobs_workflow
    assert "sessionStorage" not in jobs_workflow
    assert "indexedDB" not in jobs_workflow


def test_workspace_infrastructure_panel_is_v2_only_and_ported_faithfully():
    """DG-UI-UNIFICATION v1 U4: the legacy Infrastructure panel (workers /
    server-config / inventory / codex runner status) migrated into the v2
    Workspace as thin `/api/v2` wrappers -- nav entry, section, empty state,
    reviewed-path allowlist additions, and the exact codex-runner branch
    order all pinned so a future edit cannot silently drop one."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    infra_workflow = javascript[
        javascript.index("// ---- 基礎設施（DG-UI-UNIFICATION v1 U4）") : javascript.index(
            "function renderApprovals()"
        )
    ]

    assert 'data-section="infrastructure"' in html
    assert 'id="section-infrastructure" data-workspace-section="infrastructure" hidden' in html
    assert 'id="infra-workers-tbody"' in html
    assert 'id="infra-server-form"' in html
    assert 'id="infra-server-name"' in html
    assert 'id="infra-server-key"' in html
    assert 'id="infra-idle-tbody"' in html
    assert 'id="infra-idle-hours"' in html
    assert 'id="infra-candidate-list"' in html
    assert 'id="infra-manual-add-form"' in html
    assert 'id="infra-import-form"' in html
    assert 'id="infra-codex-status"' in html
    assert "Server A 上的私鑰路徑，不會上傳內容" in html
    assert "尚未設定任何機器" in infra_workflow
    assert "沒有已設定的機器" in infra_workflow
    assert "沒有候選專案" in infra_workflow

    #: Reviewed-path allowlist additions -- literal servers/server-configs/
    #: inventory-candidates/codex-runner paths plus the id-scoped regexes.
    for literal_path in (
        '"/api/v2/servers",',
        '"/api/v2/servers/idle-summary",',
        '"/api/v2/server-configs",',
        '"/api/v2/inventory/candidates",',
        '"/api/v2/codex-runner/status",',
        '"/api/v2/inventory/scan-requests",',
        '"/api/v2/inventory/candidates/ignore-nested-requests",',
    ):
        assert literal_path in javascript
    assert "INFRA_SERVER_CONFIG_DETAIL_PATH" in javascript
    assert "INFRA_SERVER_CONFIG_MUTATION_PATH" in javascript
    assert "INFRA_CANDIDATE_MUTATION_PATH" in javascript
    assert "INFRA_SERVER_CONFIG_DETAIL_PATH.test(parsed.pathname)" in javascript
    assert "INFRA_SERVER_CONFIG_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert "INFRA_CANDIDATE_MUTATION_PATH.test(parsed.pathname)" in javascript

    #: Section-activation load, not a global poll timer.
    assert 'if (section === "infrastructure" && state.me) {' in javascript
    assert "setInterval" not in infra_workflow

    #: Coding Runner branch order ported verbatim from
    #: `renderCodingRunnerInfrastructureStatus()` (`static/index.html`
    #: :6849-6908); busy is a success state, not "unavailable".
    assert "function codexRunnerStatusView(status, connectionFailed)" in features
    assert "Coding Runner 狀態端點無法連線" in features
    assert "Coding Runner 尚未設定" in features
    assert "Coding Runner 離線" in features
    assert "Codex 能力探測失敗" in features
    assert "Codex 尚未安裝" in features
    assert "Codex 尚未登入" in features
    assert "Coding Runner 忙碌；仍可送出核准並等待排程" in features
    assert "Coding Runner 可用" in features
    assert "busy 不等於 unavailable" in features
    assert ".innerHTML" not in infra_workflow
    assert "localStorage" not in infra_workflow
    assert "sessionStorage" not in infra_workflow
    assert "indexedDB" not in infra_workflow


def test_workspace_dataset_publish_uses_preview_then_human_approval_contract():
    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    source_controls = javascript[
        javascript.index("function datasetPublishLocalPathEnabled()") : javascript.index(
            "function datasetPublishRequestBody()"
        )
    ]
    publish_workflow = javascript[
        javascript.index("function datasetPublishRequestBody()") : javascript.index(
            "function renderSessions()"
        )
    ]

    for field_id in (
        "dataset-publish-project",
        "dataset-publish-source-kind",
        "dataset-publish-plan-id",
        "dataset-publish-output-name",
        "dataset-publish-collection",
        "dataset-publish-processing",
        "dataset-publish-counts",
        "dataset-publish-preview-btn",
        "dataset-publish-request-btn",
    ):
        assert f'id="{field_id}"' in html
    assert 'type="text"\n                     maxlength="4096" autocomplete="off"' in html
    assert 'id="dataset-publish-source-local-path" value="local_path" disabled' in html
    assert '<option value="run_output" selected>' in html
    assert 'id="dataset-publish-local-path-note"' in html
    assert "capability.local_path_enabled === true" in source_controls
    assert 'sourceSelect.value = "run_output"' in source_controls
    assert "localOption.disabled = !localPathEnabled" in source_controls
    assert 'sourceKind === "local_path" && !datasetPublishLocalPathEnabled()' in publish_workflow
    assert 'kind: "local_path"' in publish_workflow
    assert 'kind: "run_output"' in publish_workflow
    assert "expected_preview_digest: preview.preview_digest" in publish_workflow
    assert "state.datasetPublishRequestKey = randomUUID()" in publish_workflow
    assert 'activateSection("approvals")' in publish_workflow
    assert "localStorage" not in publish_workflow
    assert "sessionStorage" not in publish_workflow
    assert "indexedDB" not in publish_workflow


def test_workspace_requires_verified_detail_and_explicit_review_before_approve():
    """DG-UI-UNIFICATION v1 U1: the old "這是尚未遷移的相容流程" dead end is
    gone — every `VALID_APPROVAL_KINDS` member gets a review button in the
    list, and decides through either the immutable-contract flow
    (`REVIEWED_APPROVAL_KINDS`) or the generalized compatibility digest flow
    (`COMPATIBILITY_APPROVAL_KINDS`, now every legacy kind, not just
    `enqueue`)."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features_javascript = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    summary_renderer = javascript[
        javascript.index("function renderApprovals()") : javascript.index(
            "function renderApprovalDetail()"
        )
    ]
    decision_handler = javascript[
        javascript.index("async function decideReviewedApproval(") : javascript.index(
            "function renderDatasetState()"
        )
    ]

    assert 'id="approval-review-confirm"' in html
    assert 'id="approval-review-approve"' in html
    assert "loadApprovalDetail(approval.id" in summary_renderer
    assert "decideReviewedApproval(" not in summary_renderer
    sharing_declaration = javascript[
        javascript.index("const DATASET_SHARING_APPROVAL_KINDS") : javascript.index(
            "const REVIEWED_APPROVAL_KINDS"
        )
    ]
    reviewed_declaration = javascript[
        javascript.index("const REVIEWED_APPROVAL_KINDS") : javascript.index(
            "const ONE_TIME_SECRET_APPROVAL_KINDS"
        )
    ]
    assert '"dataset_alias_change_v2"' in reviewed_declaration
    for kind in (
        "dataset_share_offer_v2",
        "dataset_share_accept_v2",
        "dataset_grant_revoke_v2",
    ):
        assert f'"{kind}"' in sharing_declaration
    assert "...DATASET_SHARING_APPROVAL_KINDS" in reviewed_declaration
    assert 'const COMPATIBILITY_APPROVAL_KINDS = new Set([' in reviewed_declaration
    assert '"enqueue"' in reviewed_declaration
    #: The two concretely reported dead-end kinds, plus a sample spanning
    #: infrastructure/AI-engineering/identity/automated-dispatch, are all in
    #: the generalized compatibility set now (not just `enqueue`).
    for kind in (
        "server_update",
        "inventory_scan",
        "coding_task",
        "node_enroll",
        "service_token_issue",
        "run_profile_create",
        "agent_session_checkpoint",
    ):
        assert f'"{kind}"' in reviewed_declaration
    assert (
        "const ONE_TIME_SECRET_APPROVAL_KINDS = window.WorkspaceUI.ONE_TIME_SECRET_APPROVAL_KINDS;"
        in javascript
    )
    #: The old kind-gate (`INSPECTABLE_APPROVAL_KINDS`) and its dead-end
    #: fallback text/link are gone entirely — every card renders a review
    #: button, never a "尚未遷移" message and never a link to the retired
    #: legacy surface.
    assert "INSPECTABLE_APPROVAL_KINDS" not in javascript
    assert "這是尚未遷移的相容流程" not in javascript
    assert "/static/index.html" not in summary_renderer
    assert 'node("a", "前往管理核准頁"' not in summary_renderer
    assert "window.WorkspaceUI.KIND_LABEL[approval.kind]" in summary_renderer
    assert "window.WorkspaceUI.approvalCategoryLabel(approval.kind)" in summary_renderer
    assert "REVIEWED_APPROVAL_KINDS.has(detail.kind)" in decision_handler
    assert "COMPATIBILITY_APPROVAL_KINDS.has(detail.kind)" in decision_handler
    assert "DATASET_SHARING_APPROVAL_KINDS.has(detail.kind)" in decision_handler
    assert 'detail.kind === "dataset_alias_change_v2"' in decision_handler
    assert 'detail.review_mode === "compatibility_snapshot"' in decision_handler
    assert "payloadDigest: compatibilitySnapshot ? detail.payload_digest : null" in decision_handler
    assert 'decision === "approve" && !state.approvalDetailReviewed' in decision_handler
    #: `ONE_TIME_SECRET_APPROVAL_KINDS` (service_token_issue/node_enroll/
    #: node_rotate): approve refuses client-side too (defense in depth —
    #: the backend independently refuses it) while reject stays reachable.
    assert (
        'ONE_TIME_SECRET_APPROVAL_KINDS.has(detail.kind)) return;'
        in decision_handler
    )
    assert (
        "!state.approvalDetailReviewed || state.approvalDetailOneTimeSecret"
        in javascript
    )
    assert 'window.WorkspaceUI.renderApprovalSummary(summary, detail);' in javascript
    assert 'headers["X-Approval-Payload-Digest"] = options.payloadDigest' in javascript
    assert 'window.location.replace("/#section/approvals")' in LEGACY_HTML.read_text(
        encoding="utf-8"
    )
    assert 'fetch("/approvals' not in javascript

    #: `workspace-features.js` — the ported Chinese kind labels/per-kind
    #: summary source of truth, handed off via `window.WorkspaceUI` (mirrors
    #: `window.DispatchUI` in `static/ui.js`).
    assert "window.WorkspaceUI = Object.freeze({" in features_javascript
    assert "const KIND_LABEL = Object.freeze({" in features_javascript
    assert 'server_update: "更新伺服器"' in features_javascript
    assert 'inventory_scan: "掃描候選專案"' in features_javascript
    assert "function buildApprovalSummaryNodes(" in features_javascript
    assert ".innerHTML" not in features_javascript
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        assert storage not in features_javascript
    assert "http://" not in features_javascript
    assert "https://" not in features_javascript
