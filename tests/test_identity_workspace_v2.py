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
WORKSPACE_CSS = ROOT / "static" / "workspace.css"

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


def test_v2_root_is_login_first_notice_when_flag_off(api_client):
    """DG-UI-UNIFICATION v1 U8 (root cutover), extended for login-first root:
    legacy `static/index.html` is deleted, so `GET /` no longer has a legacy
    surface to roll back to. `API_V2_ENABLED` off always serves the minimal
    inline Chinese notice regardless of authentication state -- every panel
    depends on `/api/v2/*`, and this route never 404s/500s -- while every
    `/api/v2/*` route keeps 404ing exactly as before."""

    client, main_module = api_client

    hidden = client.get("/api/v2/me")
    notice_root = client.get("/")

    assert hidden.status_code == 404
    assert hidden.headers["Cache-Control"] == "no-store"
    assert hidden.headers["Pragma"] == "no-cache"
    assert notice_root.status_code == 200
    assert notice_root.headers["Cache-Control"] == "no-store"
    assert "v2 API 未啟用，請設定 API_V2_ENABLED=true" in notice_root.text
    assert 'id="workspace-navigation"' not in notice_root.text

    main_module.app_state.config.oidc_enabled = True
    main_module.app_state.config.auth_token = "configured-but-v2-hidden"
    still_hidden = client.get("/api/v2/me")
    assert still_hidden.status_code == 404
    assert still_hidden.headers["X-OIDC-Enabled"] == "true"
    main_module.app_state.config.oidc_enabled = False
    main_module.app_state.config.auth_token = None


def test_v2_root_serves_login_page_when_unauthenticated_and_workspace_once_signed_in(
    api_client,
):
    """Login-first root: an unauthenticated `GET /` never exposes the
    Workspace shell (only its data is API-protected before this change --
    the shell itself was not). It now serves `static/login.html` instead,
    and only a resolved session/service/legacy credential -- the exact same
    `resolve_request_context()` helper `auth_middleware` uses -- unlocks the
    Workspace shell. `GET /` remains exempt from the 401 gate either way
    (INV-APPROVAL-5): both variants return 200."""

    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True

    anonymous_root = client.get("/")
    anonymous = client.get("/api/v2/me")

    assert anonymous_root.status_code == 200
    assert anonymous_root.headers["Cache-Control"] == "no-store"
    assert "使用 OIDC 登入" in anonymous_root.text
    assert 'id="workspace-navigation"' not in anonymous_root.text
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_required"
    assert anonymous.headers["Cache-Control"] == "no-store"
    assert anonymous.headers["X-OIDC-Enabled"] == "false"

    _create_human(main_module.app_state.db)
    _session_for(client, main_module, ACTOR_ID)
    v2_root = client.get("/")

    assert v2_root.status_code == 200
    assert v2_root.headers["Cache-Control"] == "no-store"
    assert 'id="workspace-navigation"' in v2_root.text
    assert (
        "/static/workspace.js?v=20260827-approvals-autorefresh"
        in v2_root.text
    )

    # API_V2_ENABLED off still wins over an authenticated session -- the
    # notice precedence check is independent of auth state.
    main_module.app_state.config.api_v2_enabled = False
    reverted_root = client.get("/")
    assert reverted_root.status_code == 200
    assert "v2 API 未啟用，請設定 API_V2_ENABLED=true" in reverted_root.text
    assert client.get("/api/v2/events").status_code == 404
    assert client.get("/api/v2/audit").status_code == 404


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


def test_stale_server_update_snapshot_rejects_with_honest_reason(api_client, tmp_path):
    """#155/#157 follow-up (DG-INFRA-DIRECT-ACTIONS v1, 2026-08-26 ruling):
    two `server_update` approvals pinned against the same before-snapshot --
    deciding the first activates it; deciding the second through the v2
    generic compatibility decision path must fail with an honest, actionable
    reason (`設定已被其他變更修改（快照過期），請重新發起`) instead of the
    old opaque "could not be applied", while the error `code` stays the
    stable `compatibility_decision_failed`."""
    from app.approvals import request_server_update_approval
    from app.identity import RequestContext

    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    admin = _create_human(database, platform_admin=True)
    requester = _create_human(database, actor_id=OTHER_ACTOR_ID, name="Requester")

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    key_path = ssh_dir / "id_test"
    key_path.write_text("fake-private-key-content\n")
    key_path.chmod(0o600)

    payload = {
        "name": "server-x",
        "host": "10.0.0.5",
        "user": "train",
        "key": str(key_path),
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
            project_roots=list(payload["project_roots"]),
            dataset_roots=list(payload["dataset_roots"]),
            enabled=payload["enabled"],
        )
    }
    current_document = load_servers_config(main_module.app_state.config.servers_yaml_path)
    requester_context = RequestContext(actor=requester)

    #: Both approvals pin the *same* before-snapshot -- exactly the #155/
    #: #157 shape (two cards reviewed against one servers.yaml revision).
    approval_1 = request_server_update_approval(
        database,
        "server-x",
        {"host": "10.0.0.11"},
        main_module.app_state.config,
        current_document["servers"],
        audit_path=main_module.app_state.config.audit_path,
        request_context=requester_context,
        current_document=current_document,
    )
    approval_2 = request_server_update_approval(
        database,
        "server-x",
        {"host": "10.0.0.22"},
        main_module.app_state.config,
        current_document["servers"],
        audit_path=main_module.app_state.config.audit_path,
        request_context=requester_context,
        current_document=current_document,
    )

    _session_for(client, main_module, admin.id)

    first_detail = client.get(f"/api/v2/approvals/{approval_1.id}").json()
    first_decision = client.post(
        f"/api/v2/approvals/{approval_1.id}/decisions",
        json={"decision": "approve", "note": "reviewed first"},
        headers={
            "Idempotency-Key": "stale-server-update-first",
            "X-Approval-Payload-Digest": first_detail["payload_digest"],
        },
    )
    assert first_decision.status_code == 202
    assert first_decision.json()["status"] == "approved"

    second_detail = client.get(f"/api/v2/approvals/{approval_2.id}").json()
    second_decision = client.post(
        f"/api/v2/approvals/{approval_2.id}/decisions",
        json={"decision": "approve", "note": "reviewed second"},
        headers={
            "Idempotency-Key": "stale-server-update-second",
            "X-Approval-Payload-Digest": second_detail["payload_digest"],
        },
    )

    assert second_decision.status_code == 409
    error = second_decision.json()["error"]
    assert error["code"] == "compatibility_decision_failed"
    assert "設定已被其他變更修改（快照過期），請重新發起" in error["message"]
    #: The friendly text *is* `details.reason` now (both come straight from
    #: `str(ServerPublicationRejected(...))`) -- no separate opaque code
    #: leaks through either field.
    assert "設定已被其他變更修改（快照過期），請重新發起" in error["details"]["reason"]
    #: 沒被第二筆蓋掉：第一筆的結果留著。
    on_disk = load_servers_config(main_module.app_state.config.servers_yaml_path)
    assert on_disk["servers"][0]["host"] == "10.0.0.11"
    assert database.get_approval(approval_2.id).status == "pending"


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
    combined = "\n".join((html, javascript))

    assert (
        'href="/static/workspace.css?v=20260827-approvals-autorefresh"'
        in html
    )
    assert (
        'src="/static/workspace-features.js?v=20260827-approvals-autorefresh"'
        in html
    )
    assert (
        'src="/static/workspace.js?v=20260827-approvals-autorefresh"'
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


def test_workspace_wide_tables_are_responsive_below_the_680px_breakpoint():
    """Wide data tables (工作機／工作／Runs／AI 工程任務／專案×伺服器矩陣／
    程式版本／閒置摘要) stop requiring horizontal scrolling on narrow screens:
    every `table-wrap` wrapping one of them carries the `responsive` opt-in
    class, `workspace.css` defines the stacked-card media query keyed off
    `data-label`, and at least one JS row-builder actually sets the
    attribute (not just declares a header array) so the CSS has something to
    render."""
    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    css = WORKSPACE_CSS.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")

    for tbody_id in (
        "legacy-matrix-tbody",
        "legacy-project-detail-versions-tbody",
        "run-list",
        "jobs-tbody",
        "engineering-tasks-tbody",
        "infra-workers-tbody",
        "infra-idle-tbody",
        # Packet D3 (usage accounting): the 使用量 table joins the
        # responsive-table cohort.
        "ai-providers-usage-tbody",
    ):
        assert f'id="{tbody_id}"' in html
    assert html.count('class="table-wrap responsive"') == 8

    assert "content: attr(data-label)" in css
    assert ".table-wrap.responsive" in css

    assert "function applyDataLabels(row, headers)" in javascript
    assert 'applyDataLabels(row, TABLE_HEADERS.infraWorkers)' in javascript
    assert 'applyDataLabels(row, TABLE_HEADERS.jobs)' in javascript
    assert 'applyDataLabels(row, TABLE_HEADERS.aiProvidersUsage)' in javascript


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
    #: DG-UI-UNIFICATION v1 U6a added "engineering" to this closed-vocabulary
    #: hash-route regex (`sectionFromHash()`).
    assert (
        "overview|projects|project-bootstrap|runs|jobs|engineering|infrastructure|legacy-datasets|approvals|datasets|sessions"
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
    #: DG-UI-UNIFICATION v1 U6a replaced this dead placeholder with a real
    #: link into the new AI 工程 section (see the U6a-pinned test below).
    assert "查看 AI 工程任務" in jobs_workflow
    assert "尚未實作連結" not in jobs_workflow
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
    #: U1 gap fix: `project_role_change` is a Product v2 immutable contract
    #: kind (`app.db.TRANSACTION_ONLY_APPROVAL_KINDS`) that gets the exact
    #: same verified-detail shape as its `_v2`-suffixed siblings via
    #: `dispatch_center/api/routers/project_roles_v2.py`'s
    #: `decide_project_role_change` fallback branch, but had been carved out
    #: of `REVIEWED_APPROVAL_KINDS` -- it decides through the identical
    #: reviewed flow now, with its own approve-button label and Chinese
    #: summary (not the generic "尚無專用摘要" fallback).
    assert '"project_role_change"' in reviewed_declaration
    approve_label_block = javascript[
        javascript.index("const approveLabels = {") : javascript.index(
            "};", javascript.index("const approveLabels = {")
        )
    ]
    assert 'project_role_change: "核准角色變更"' in approve_label_block
    assert "project_role_change(p) {" in features_javascript
    assert '["目標 Actor ID"' in features_javascript
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
    #: DG-UI-UNIFICATION v1 U8: the legacy `#approval/<id>` -> `#section/
    #: approvals` inline redirect guard is retired along with
    #: `static/index.html` itself -- there is no second surface left to
    #: redirect away from.
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


def test_workspace_legacy_projects_and_datasets_panel_is_v2_only_and_ported_faithfully():
    """DG-UI-UNIFICATION v1 U5: the legacy Projects list/detail (excluding
    the ai-engineering pane -- U6) and Datasets & Results panels migrated
    into the v2 Workspace, merged with the existing typed Projects section --
    nav entry, new legacy-datasets section, reviewed-path allowlist
    additions, and the project-detail sub-tab set all pinned so a future
    edit cannot silently drop one."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    legacy_projects_workflow = javascript[
        javascript.index("function renderProjects()") : javascript.index(
            "function renderRuns()"
        )
    ]

    assert 'data-section="legacy-datasets"' in html
    assert 'id="section-legacy-datasets" data-workspace-section="legacy-datasets" hidden' in html
    assert 'id="legacy-project-create-form"' in html
    assert 'id="legacy-project-create-name"' in html
    assert 'id="legacy-project-create-repo"' in html
    assert 'id="legacy-matrix-tbody"' in html
    assert 'id="legacy-project-detail-panel"' in html
    for tab in ("overview", "versions", "data", "settings", "deploy", "timeline", "activity"):
        assert f'data-legacy-detail-tab="{tab}"' in html
        assert f'data-legacy-detail-panel="{tab}"' in html
    assert 'id="legacy-project-detail-goal"' in html
    assert 'id="legacy-project-detail-progress"' in html
    assert 'id="legacy-project-detail-versions-tbody"' in html
    assert 'id="legacy-project-record-form"' in html
    assert 'id="legacy-project-timeline-kinds"' in html
    assert 'id="legacy-project-activity-probe-btn"' in html
    assert 'id="legacy-dataset-list"' in html
    assert 'id="legacy-dataset-create-form"' in html
    assert 'id="legacy-dataset-create-description"' in html
    assert 'id="legacy-dataset-create-method"' in html
    assert 'id="legacy-dataset-card-panel"' in html
    assert 'id="legacy-dataset-card-update-form"' in html
    #: memberships/run-profiles/dispatch-policies deferral note, per U5 scope.
    assert "尚未遷移" in html

    #: `renderLegacyDatasets()`: 無卡版本標「無資料卡」；補登走同一個
    #: `legacy-dataset-card-update-form`/`.../card` 端點（ported from legacy
    #: `renderDatasets()` labels, `static/index.html` 階段 16）.
    render_legacy_datasets = javascript[
        javascript.index("function renderLegacyDatasets()") : javascript.index(
            "async function openLegacyDatasetCard("
        )
    ]
    assert "無資料卡" in render_legacy_datasets
    assert "檢視資料卡" in render_legacy_datasets
    assert "更新／補登資料卡" in html

    #: matrix "pending import candidates" nudge (ported from legacy
    #: `renderProjectsMatrix()`'s equivalent note, `static/index.html` 階段
    #: 15) -- routes to 基礎設施/Inventory instead of a dead legacy tab link.
    assert '尚有候選未處理' in javascript
    assert 'activateSection("infrastructure")' in javascript

    #: Reviewed-path allowlist additions.
    for literal_path in (
        '"/api/v2/legacy-projects",',
        '"/api/v2/projects-matrix",',
        '"/api/v2/legacy-datasets",',
    ):
        assert literal_path in javascript
    assert "LEGACY_PROJECT_READ_PATH" in javascript
    assert "LEGACY_PROJECT_MUTATION_PATH" in javascript
    assert "LEGACY_PROJECT_RECORD_MUTATION_PATH" in javascript
    assert "LEGACY_PROJECT_ACTION_MUTATION_PATH" in javascript
    assert "LEGACY_DATASET_CARD_PATH" in javascript
    assert "LEGACY_PROJECT_READ_PATH.test(parsed.pathname)" in javascript
    assert "LEGACY_PROJECT_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert "LEGACY_DATASET_CARD_PATH.test(parsed.pathname)" in javascript
    #: `productMutation` gained an `options.method` parameter (first mutation
    #: methods beyond POST) without changing its default or the pinned
    #: `body: JSON.stringify(body)` call.
    assert 'const method = (options && options.method) || "POST";' in javascript
    assert "method,\n      credentials:" in javascript
    assert 'body: JSON.stringify(body)' in javascript

    #: Section-activation load, not a global poll timer, mirroring U3/U4.
    assert (
        'if (section === "projects" && state.me && !state.legacyProjectsSummaryLoaded) {'
        in javascript
    )
    assert 'if (section === "legacy-datasets" && state.me) loadLegacyDatasets();' in javascript
    assert "setInterval" not in legacy_projects_workflow

    #: v2 typed projection stays authoritative for which project cards show;
    #: legacy facts are merged onto them, never replace them.
    assert "state.legacyProjectsByName[project.name]" in legacy_projects_workflow
    assert "openLegacyProjectDetail(project.name)" in legacy_projects_workflow
    assert "jumpToJobDispatch(project.name)" in legacy_projects_workflow
    assert "deleteLegacyProjectAction(project.name)" in legacy_projects_workflow
    assert 'window.confirm(' in legacy_projects_workflow
    assert "window.WorkspaceUI.instanceStateLabel(instance.state)" in legacy_projects_workflow
    assert "function instanceStateLabel(value)" in features
    assert 'available: "可用"' in features
    assert 'diverged: "分歧"' in features

    #: U5 baseline UX simplification: plain-text `<pre>`/`textContent`, not
    #: the legacy escape-first `renderMarkdown` (see U5 completion report).
    assert (
        'element("legacy-project-detail-goal").textContent = project.goal'
        in legacy_projects_workflow
    )
    assert ".innerHTML" not in legacy_projects_workflow
    assert "localStorage" not in legacy_projects_workflow
    assert "sessionStorage" not in legacy_projects_workflow
    assert "indexedDB" not in legacy_projects_workflow


def test_workspace_ai_engineering_panel_is_v2_only_and_instruction_contract_is_pinned():
    """DG-UI-UNIFICATION v1 U6a: the AI 工程 section (engineering-task
    wizard, task detail, coding runs) migrated into the v2 Workspace as a
    new nav entry -- allowlist additions, tab set, wizard fields, and the
    machine-readable instruction-text contract all pinned so a future edit
    cannot silently drop or reword them (the English text is sent to the
    agent and is pinned server-side too, see
    tests/test_engineering_tasks.py::test_structured_renderer_has_fixed_order_and_normalizes_items)."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    engineering_workflow = javascript[
        javascript.index("function engineeringTaskStatusCode(") : javascript.index(
            "// ---- 基礎設施（DG-UI-UNIFICATION v1 U4）"
        )
    ]

    #: Nav entry + section.
    assert 'data-section="engineering"' in html
    assert 'id="section-engineering" data-workspace-section="engineering" hidden' in html

    #: Task list.
    assert 'id="engineering-tasks-tbody"' in html
    assert 'id="engineering-refresh-btn"' in html
    assert 'id="engineering-create-btn"' in html

    #: Wizard (5 grouped sections, single-page form -- see
    #: `engineering_v2.py`/workspace.js module docstrings for why this is a
    #: scrollable multi-section form rather than the legacy paginated next/
    #: back flow).
    assert 'id="engineering-wizard-panel"' in html
    assert 'id="engineering-wizard-project"' in html
    assert 'id="engineering-wizard-provider"' in html
    assert 'id="engineering-wizard-version"' in html
    assert 'id="engineering-wizard-objective"' in html
    assert 'id="engineering-wizard-allowed-paths"' in html
    assert 'id="engineering-wizard-coverage-btn"' in html
    assert 'id="engineering-wizard-tests"' in html
    assert 'id="engineering-wizard-validation-target"' in html
    assert 'id="engineering-wizard-instruction-preview"' in html
    assert 'id="engineering-wizard-instruction-count"' in html
    assert 'id="engineering-wizard-submit-btn"' in html
    #: Fixed (not user-editable) V1 permissions rendered as static Chinese
    #: text with the enforcement badges, never as input checkboxes.
    assert 'id="engineering-wizard-fixed-permissions"' in html
    assert "技術強制" in html
    assert "後續版本" in html

    #: Task detail: 8 tabs.
    for tab in (
        "overview", "timeline", "commands", "changes", "tests", "artifacts", "risks", "approvals",
    ):
        assert f'data-task-tab="{tab}"' in html
        assert f'data-task-panel="{tab}"' in html
    assert 'id="engineering-task-retry-btn"' in html
    assert 'id="engineering-task-discard-btn"' in html
    assert 'id="engineering-task-promote-btn"' in html
    assert 'id="engineering-task-download-patch-btn"' in html
    assert 'id="engineering-task-cleanup-btn"' in html
    assert 'id="engineering-task-validation-btn"' in html
    #: Future/unsupported actions stay disabled with the original legacy
    #: reasons, never silently hidden.
    assert "此動作需要後續受控執行切片" in html
    assert "原始 bundle 可能含未去敏內容，平台不提供下載" in html

    #: Reviewed-path allowlist additions.
    for literal_path in (
        '"/api/v2/engineering-tasks",',
        '"/api/v2/engineering-tasks/capabilities",',
        '"/api/v2/coding-agents",',
        '"/api/v2/coding-runs",',
    ):
        assert literal_path in javascript
    assert "ENGINEERING_TASK_READ_PATH" in javascript
    assert "ENGINEERING_TASK_PATCH_PATH" in javascript
    assert "ENGINEERING_TASK_MUTATION_PATH" in javascript
    assert "CODING_RUN_READ_PATH" in javascript
    assert "CODING_RUN_MUTATION_PATH" in javascript
    assert "LEGACY_PROJECT_ENGINEERING_MUTATION_PATH" in javascript
    assert "ENGINEERING_TASK_READ_PATH.test(parsed.pathname)" in javascript
    assert "ENGINEERING_TASK_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert "CODING_RUN_READ_PATH.test(parsed.pathname)" in javascript
    assert "CODING_RUN_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert "LEGACY_PROJECT_ENGINEERING_MUTATION_PATH.test(parsed.pathname)" in javascript

    #: The `/patch` download route is deliberately excluded from
    #: `productRead`'s JSON allowlist -- it is a binary/text download,
    #: ported through `sameOriginDownloadPath()`/
    #: `authenticatedEngineeringPatchDownload()` (mirrors legacy
    #: `sameOriginDownloadPath()`/`authenticatedDownload()`). DG-UI-
    #: UNIFICATION v1 U8: this is the equivalent protection for the deleted
    #: `tests/test_frontend_auth.py::
    #: test_authenticated_patch_download_preserves_auth_and_generation_guards`
    #: pin -- every download-safety property that test asserted is checked
    #: here directly against the ported function bodies (same-origin path
    #: validation, no embedded credentials/hash, `credentials: "same-
    #: origin"`/`cache: "no-store"`, JSON-detail unwrap on failure, the two
    #: response-header semantics checks, and the blob size bound).
    download_helpers = javascript[
        javascript.index("function sameOriginDownloadPath(path)") : javascript.index(
            "function showAlert(message)"
        )
    ]
    assert "function sameOriginDownloadPath(path)" in download_helpers
    assert "async function authenticatedEngineeringPatchDownload(path)" in download_helpers
    assert '!path.startsWith("/") || path.startsWith("//")' in download_helpers
    assert "parsed.origin !== window.location.origin" in download_helpers
    assert "parsed.username ||" in download_helpers
    assert "parsed.password ||" in download_helpers
    assert "parsed.hash ||" in download_helpers
    assert "normalized !== path" in download_helpers
    assert 'credentials: "same-origin"' in download_helpers
    assert 'cache: "no-store"' in download_helpers
    assert 'contentType.startsWith("application/json")' in download_helpers
    assert 'response.headers.get("X-Engineering-Patch-Redacted")' in download_helpers
    assert 'redactedHeader !== "true" && redactedHeader !== "false"' in download_helpers
    assert 'response.headers.get("X-Artifact-Semantics")' in download_helpers
    assert '"sanitized-collected-patch"' in download_helpers
    assert 'responseContentType !== "text/x-diff"' in download_helpers
    assert "await response.blob()" in download_helpers
    assert "blob.size < 1 || blob.size > 1024 * 1024" in download_helpers

    #: Section-activation load, not a global poll timer, mirroring U3/U4/U5.
    assert 'if (section === "engineering" && state.me) loadEngineeringTasks();' in javascript
    assert "setInterval" not in engineering_workflow

    #: U3's jobs-row placeholder now navigates to this section and opens the
    #: owning task/run detail instead of a dead "尚未實作連結" label.
    assert "查看 AI 工程任務（尚未實作連結）" not in javascript
    assert 'activateSection("engineering")' in javascript
    assert "openEngineeringTaskDetail(taskId)" in javascript

    #: `available_actions`-driven action bar -- server capability is the
    #: only source of truth for which buttons are enabled.
    assert "window.WorkspaceUI.engineeringAction(task, " in javascript
    assert "retryBtn.disabled = !(retry && retry.enabled === true);" in javascript

    assert ".innerHTML" not in engineering_workflow
    assert "localStorage" not in engineering_workflow
    assert "sessionStorage" not in engineering_workflow
    assert "indexedDB" not in engineering_workflow

    #: Instruction-contract pin (workspace-features.js): the English text is
    #: a machine contract sent to the agent, byte-identical to the legacy
    #: `static/ui.js` renderer -- pin exact sentences from every section so a
    #: future "helpful" localization or rewording cannot silently change what
    #: the agent receives.
    assert "function renderEngineeringTaskInstruction(values, { enforceFinalGitPaths = false } = {}) {" in features
    assert 'const parts = ["AI Engineering Task"];' in features
    assert (
        "Run the repository's relevant tests and lint checks only inside this "
        "current sandboxed agent turn; the outer Runner will not execute "
        "repository code after the turn."
    ) in features
    assert "Modify project files only inside the existing isolated Git worktree." in features
    assert (
        "Dependency installation is not authorized by this form; "
        "do not install dependencies."
    ) in features
    assert (
        "External network access is not authorized by this form; "
        "do not access external networks."
    ) in features
    assert (
        "The final Git diff is technically checked against the approved path "
        "policy on the Runner before bundling and independently on Server A "
        "before acceptance."
    ) in features
    assert "const ENGINEERING_INSTRUCTION_LIMIT = 4000;" in features
    assert ".innerHTML" not in features
    for storage in ("localStorage", "sessionStorage", "indexedDB"):
        assert storage not in features


def test_workspace_ai_engineer_tab_is_ported_faithfully_with_pinned_behaviors():
    """DG-UI-UNIFICATION v1 U6b: the AgentSession Development Session
    workbench (DG-AGENT-SESSION-V1 P4) + per-project AI conversation (DG-
    CONVERSATION-V1 CV-2a) migrated into a new "AI Engineer" tab inside the
    U5 project detail panel. Legacy files/pinned tests
    (tests/test_agent_session_ui.py) stay untouched -- this pins the
    equivalent behaviors re-implemented in the workspace files."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")
    agent_session_workflow = javascript[
        javascript.index("function agentSessionShowPanel(") : javascript.index(
            "  // ---------------------------------------------------------------------\n  // 資料集（legacy 相容"
        )
    ]

    #: New tab in the U5 project detail tab bar, between 總覽 and 程式版本.
    assert 'data-legacy-detail-tab="ai-engineer">AI Engineer</button>' in html
    assert 'data-legacy-detail-panel="ai-engineer" hidden>' in html

    #: Development Session workbench markup: open form, workbench, turn
    #: panel, diff, checkpoint/promote bridge.
    assert 'id="legacy-agent-session-section" hidden>' in html
    assert 'id="legacy-agent-session-version"' in html
    assert 'id="legacy-agent-session-open-btn"' in html
    assert 'id="legacy-agent-session-workbench" hidden>' in html
    assert 'id="legacy-agent-session-turns"' in html
    assert 'id="legacy-agent-session-close-btn"' in html
    assert 'id="legacy-agent-session-live-status-text"' in html
    assert 'id="legacy-agent-session-input" rows="3" maxlength="65536"' in html
    assert 'id="legacy-agent-session-diff-btn"' in html
    assert 'id="legacy-agent-session-checkpoint-btn"' in html
    assert 'id="legacy-agent-session-promote-btn"' in html
    #: 技術細節 stays a collapsed `<details>` (no `open` attribute) so raw
    #: transcript JSON never becomes the primary reading surface.
    assert '<details id="legacy-agent-session-turn-tech-details">' in html
    assert '<details id="legacy-agent-session-turn-tech-details" open>' not in html

    #: Per-project AI conversation (CV-2a) markup.
    assert 'id="legacy-ai-conversation-section" hidden>' in html
    assert 'id="legacy-ai-conversation-messages"' in html
    assert 'id="legacy-ai-conversation-input" rows="3" maxlength="65536"' in html

    #: Reviewed-path allowlist additions (read + mutation regexes, plus the
    #: reused U1 `/api/v2/approvals` list literal for the checkpoint/promote
    #: bridge poll loops).
    assert '"/api/v2/approvals",' in javascript
    assert "LEGACY_PROJECT_AI_ENGINEER_READ_PATH" in javascript
    assert "LEGACY_PROJECT_AI_ENGINEER_MUTATION_PATH" in javascript
    assert "AGENT_SESSION_READ_PATH" in javascript
    assert "AGENT_SESSION_MUTATION_PATH" in javascript
    assert "LEGACY_PROJECT_AI_ENGINEER_READ_PATH.test(parsed.pathname)" in javascript
    assert "LEGACY_PROJECT_AI_ENGINEER_MUTATION_PATH.test(parsed.pathname)" in javascript
    assert "AGENT_SESSION_READ_PATH.test(parsed.pathname)" in javascript
    assert "AGENT_SESSION_MUTATION_PATH.test(parsed.pathname)" in javascript

    #: Serial-guard pattern for the three independent poll loops (turn
    #: transcript, checkpoint approval, promote approval) -- at least two
    #: guard checks per loop (before the request and after every await),
    #: mirroring the legacy `pollSerial` convention.
    assert javascript.count("!== state.agentSessionPollSerial") >= 2
    assert javascript.count("!== state.agentSessionCheckpointPollSerial") >= 2
    assert javascript.count("!== state.agentSessionPromotePollSerial") >= 2
    #: Turn offset advances by UTF-8 byte length, not string length.
    assert "state.agentSessionPollOffset += new TextEncoder().encode(data.transcript_chunk).length;" in javascript

    #: Degraded states rendered as readable Chinese text, not thrown errors.
    assert "Runner 暫時無法連線，session 維持 active，稍後自動重試。" in javascript
    assert "連不上執行機，稍後再試。" in javascript
    assert "無法連線到 Runner，session 維持 active，可稍後重試。" in javascript

    #: The friendly tool-event text port (with its emoji map) and the plain-
    #: language per-tool live-status line both live in workspace-features.js.
    assert "function agentSessionFriendlyToolEventText(name, input) {" in features
    assert "📖 讀取" in features
    assert "✏️ 修改" in features
    assert "🔍 搜尋" in features
    assert "⚙️ 執行指令" in features
    assert "🔧 ${name}" in features
    assert "function agentSessionLiveStatusForTool(name, input) {" in features
    assert "Claude 正在思考…" in javascript

    #: Transcript-line JSON parsing never throws (malformed/partial line ->
    #: null).
    assert "function agentSessionParseTranscriptLine(line) {" in features
    assert "return null;" in features

    #: Checkpoint -> promote bridge: never auto-approved, both actions
    #: gated behind `window.confirm()`.
    assert "async function submitAgentSessionCheckpoint() {" in javascript
    assert "async function submitAgentSessionPromote() {" in javascript
    checkpoint_and_promote = javascript[
        javascript.index("async function submitAgentSessionCheckpoint() {") : javascript.index(
            "// -------------------------------------------------------------------\n  // DG-CONVERSATION-V1 CV-2a/CV-5"
        )
    ]
    assert checkpoint_and_promote.count("window.confirm(") == 2
    assert "永不自動核准；請至「核准」頁審核。" in checkpoint_and_promote
    assert "agentSessionExtractBridgeTaskId" in javascript

    #: Reset-on-project-switch wiring: both sub-panels reset then reload on
    #: every `openLegacyProjectDetail()` call, and reset again on close.
    assert "resetAgentSessionPanel();\n    resetAIConversationPanel();" in javascript
    assert "loadAgentSessionPanel(projectName);\n    loadAIConversationPanel(projectName);" in javascript

    #: No raw HTML injection anywhere in the new renderers.
    assert ".innerHTML" not in agent_session_workflow
    assert "localStorage" not in agent_session_workflow
    assert "sessionStorage" not in agent_session_workflow
    assert "indexedDB" not in agent_session_workflow


def test_workspace_assistant_chat_is_ported_faithfully_with_pinned_behaviors():
    """DG-UI-UNIFICATION v1 U7: the legacy `/ws` chat client
    (`static/index.html` `connectChatSocket()`/`handleChatIncoming()`/
    `sendChatMessage()`, :6540-6810) migrated into a new 「助手」 section in
    the Product v2 Workspace, reusing the existing `/ws` endpoint (auth
    protocol unchanged, backend untouched) and the U1
    `window.WorkspaceUI.renderApprovalSummary()` summary renderer for
    in-chat approval cards instead of porting `chatApprovalCardHtml`'s
    innerHTML template."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")

    #: Nav entry + section markup.
    assert 'data-section="assistant">' in html
    assert 'id="section-assistant" data-workspace-section="assistant" hidden>' in html
    assert 'id="assistant-status"' in html
    assert 'id="assistant-messages"' in html
    assert 'id="assistant-form"' in html
    assert 'id="assistant-input"' in html
    assert 'id="assistant-send-btn"' in html

    #: `/ws` is not a `fetch()` call, so it lives outside
    #: `PRODUCT_READ_PATHS`/`PRODUCT_MUTATION_PATHS` -- pinned as its own
    #: literal constant instead, and it is the only place `new WebSocket(`
    #: is ever constructed in this file (no other WS URL is constructible).
    assert 'const CHAT_WEBSOCKET_PATH = "/ws";' in javascript
    #: Only one *code* call site constructs a WebSocket (comments above the
    #: constant also mention the literal `new WebSocket(` text for a reader,
    #: so count non-comment lines rather than the whole file).
    websocket_call_lines = [
        line for line in javascript.splitlines()
        if "new WebSocket(" in line and not line.strip().startswith("//")
    ]
    assert len(websocket_call_lines) == 1
    assert "new WebSocket(`${proto}//${window.location.host}${CHAT_WEBSOCKET_PATH}`)" in javascript

    assert "function chatSetStatus(" in javascript
    assert "function chatAppendMessage(" in javascript
    assert "function chatAppendApprovalCard(" in javascript
    assert "function chatHandleIncoming(" in javascript
    assert "function chatConnect(" in javascript
    assert "function chatStop(" in javascript
    assert "function chatSend(" in javascript

    chat_block = javascript[
        javascript.index("function chatSetStatus(") : javascript.index(
            "function renderDatasetState("
        )
    ]

    #: Backoff: reconnect delay starts at 1s, doubles, capped at 15s -- same
    #: numbers as legacy `chatReconnectDelay`.
    assert "state.chatReconnectDelay = 1000;" in chat_block
    assert "Math.min(state.chatReconnectDelay * 2, 15000)" in chat_block

    #: Auth-frame semantics unchanged: only sent when a legacy token is in
    #: play (session-cookie identity needs no first frame -- see
    #: `app/main.py` `_ws_authenticate()`'s pre-authenticated-context path).
    assert 'ws.send(JSON.stringify({ type: "auth", token: state.legacyToken }));' in chat_block
    assert "if (state.legacyToken) {" in chat_block

    #: Intentional simplification vs. legacy (docs/DECISIONS.md 2026-08-25,
    #: U7 packet): no in-chat decision path. `chatApprovalCardHtml`'s
    #: `/approve/{id}`/`/reject/{id}` calls and its innerHTML template are
    #: not ported -- decisions happen exclusively through the U1
    #: `POST /api/v2/approvals/{id}/decisions` flow in the 核准 section.
    assert "/approve/" not in chat_block
    assert "/reject/" not in chat_block
    assert "chatApprove" not in chat_block
    assert "chatReject" not in chat_block
    assert ".innerHTML" not in chat_block
    assert "localStorage" not in chat_block
    assert "sessionStorage" not in chat_block
    assert "indexedDB" not in chat_block

    #: Incoming approval cards reuse the U1 summary renderer + jump to the
    #: 核准 section via the existing `loadApprovalDetail()`, never a
    #: duplicate per-kind body template.
    assert "window.WorkspaceUI.renderApprovalSummary(summary, approval);" in chat_block
    assert '"前往核准區"' in chat_block
    assert 'activateSection("approvals");' in chat_block
    assert "loadApprovalDetail(approval.id, goToApprovals);" in chat_block

    #: `auto_approved` (web-direct-execute / rule auto-approval) collapses to
    #: a one-line status instead of a full card -- nothing left to review.
    assert "if (msg.auto_approved) {" in chat_block
    assert "已直接執行（任務 #${approval.id}）" in chat_block

    #: Section-scoped connection lifecycle: connect only on activation (not
    #: page load), stop cleanly on section leave and on logout/identity loss.
    #: DG-ASSISTANT-CLAUDE-TURN v1 C2: the same activation branch now also
    #: loads the AI 供應商 panel once (see
    #: `test_workspace_ai_providers_panel_is_pinned_with_masked_key_input`).
    assert (
        'if (section === "assistant" && state.me) {\n'
        "      chatConnect();\n"
        "      refreshAiProvidersStatus();\n"
        "    }" in javascript
    )
    assert 'if (previousSection === "assistant" && section !== "assistant") chatStop("尚未連線");' in javascript
    assert "chatStop(\"尚未登入\");\n    state.me = null;" in javascript
    assert "|assistant)$/" in javascript.split("function sectionFromHash()")[1][:400]


def test_workspace_ai_providers_panel_is_pinned_with_masked_key_input():
    """DG-ASSISTANT-CLAUDE-TURN v1 C2 (docs/DECISIONS.md 2026-08-26): the
    「AI 供應商」panel at the top of the 助手 section (Claude 訂閱（runner）/
    Anthropic API key/Codex status rows) plus the brain-mode pill above the
    chat input. The masked key input never echoes a typed value back and is
    cleared unconditionally after every submit/clear round trip."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")

    #: Panel markup, nested inside the 助手 section, above `assistant-messages`.
    assert html.index('id="ai-providers-panel"') < html.index('id="assistant-messages"')
    assert 'id="ai-providers-claude-status"' in html
    assert 'id="ai-providers-claude-note"' in html
    assert 'id="ai-providers-anthropic-status"' in html
    assert 'id="ai-providers-anthropic-form"' in html
    assert 'id="ai-providers-codex-status"' in html
    assert 'id="ai-providers-codex-note"' in html
    assert 'id="ai-providers-refresh-btn"' in html
    assert 'id="assistant-brain-pill"' in html

    #: The key input is a masked password field, never a plain text one.
    assert 'id="ai-providers-anthropic-input" type="password"' in html

    #: Reviewed-path allowlist additions.
    assert '"/api/v2/ai-providers/status",' in javascript
    assert '"/api/v2/ai-providers/anthropic-key",' in javascript

    #: Pure view functions (mirrors `codexRunnerStatusView()`'s convention).
    assert "function claudeRunnerStatusView(status, connectionFailed)" in features
    assert "請在 Runner 主機（${st.server || \"-\"}）執行 claude 並完成訂閱登入。" in features
    assert "function anthropicKeyStatusView(anthropicStatus)" in features
    assert "function assistantBrainPillText(assistantBrain)" in features
    assert "runner_claude: \"Claude 訂閱\"" in features
    assert "vllm: \"本地 vLLM\"" in features
    assert "rule_based: \"規則式\"" in features

    ai_providers_block = javascript[
        javascript.index("async function refreshAiProvidersStatus()") : javascript.index(
            "function chatSetStatus("
        )
    ]
    #: GET status feeds all four widgets from one response.
    assert 'await productRead("/api/v2/ai-providers/status")' in ai_providers_block
    assert "window.WorkspaceUI.claudeRunnerStatusView(" in ai_providers_block
    assert "window.WorkspaceUI.anthropicKeyStatusView(" in ai_providers_block
    assert "window.WorkspaceUI.codexRunnerStatusView(" in ai_providers_block
    assert "window.WorkspaceUI.assistantBrainPillText(" in ai_providers_block

    #: The masked input is cleared immediately, unconditionally, and the
    #: typed value never touches `state` (never re-populated on failure).
    assert "input.value = \"\";" in ai_providers_block
    assert 'productMutation("/api/v2/ai-providers/anthropic-key", { api_key: apiKey })' in ai_providers_block
    assert (
        'productMutation("/api/v2/ai-providers/anthropic-key", null, { method: "DELETE" })'
        in ai_providers_block
    )
    assert ".innerHTML" not in ai_providers_block
    assert "localStorage" not in ai_providers_block


def test_workspace_ai_providers_pool_model_and_usage_panel_is_pinned():
    """Packet D1/D2/D3/D4: per-runner pool lists, assistant/API model
    selection forms, the usage table, and the vLLM config-presence row."""

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")
    features = WORKSPACE_FEATURES_JS.read_text(encoding="utf-8")

    # D1: per-runner pool list markup.
    assert 'id="ai-providers-claude-runners-list"' in html
    assert 'id="ai-providers-codex-runners-list"' in html

    # D2: model selection forms.
    assert 'id="ai-providers-assistant-model-select"' in html
    assert 'id="ai-providers-assistant-model-custom-field" hidden' in html
    assert 'id="ai-providers-assistant-model-custom-input"' in html
    assert 'id="ai-providers-api-model-input"' in html
    assert '<option value="sonnet">sonnet</option>' in html
    assert '<option value="opus">opus</option>' in html
    assert '<option value="haiku">haiku</option>' in html
    assert '<option value="__custom__">自訂…</option>' in html

    # D3: usage panel markup + honest no-official-quota note.
    assert 'id="ai-providers-usage-panel"' in html
    assert 'id="ai-providers-usage-table"' in html
    assert 'id="ai-providers-usage-tbody"' in html
    assert 'id="ai-providers-usage-totals"' in html
    assert 'class="table-wrap responsive"' in html.split('id="ai-providers-usage-table"')[0][-200:]
    assert "官方剩餘額度無查詢介面" in html

    # D4: vLLM panel row.
    assert 'id="ai-providers-vllm-status"' in html
    assert 'id="ai-providers-vllm-note"' in html

    # Pure view functions.
    assert "function claudeRunnerPoolView(runners, connectionFailed)" in features
    assert "function codexRunnerPoolView(runners, connectionFailed)" in features
    assert "function vllmStatusView(vllmStatus, connectionFailed)" in features
    assert "function usageBreakdownRows(usageSummary)" in features
    assert ".innerHTML" not in features

    # Reviewed-path allowlist additions.
    assert '"/api/v2/ai-providers/usage",' in javascript
    assert '"/api/v2/ai-providers/assistant-model",' in javascript
    assert '"/api/v2/ai-providers/api-model",' in javascript

    ai_providers_block = javascript[
        javascript.index("async function refreshAiProvidersStatus()") : javascript.index(
            "function chatSetStatus("
        )
    ]
    assert "window.WorkspaceUI.claudeRunnerPoolView(" in ai_providers_block
    assert "window.WorkspaceUI.codexRunnerPoolView(" in ai_providers_block
    assert "window.WorkspaceUI.vllmStatusView(" in ai_providers_block
    assert 'productMutation("/api/v2/ai-providers/assistant-model", { model })' in ai_providers_block
    assert 'productMutation("/api/v2/ai-providers/api-model", { model })' in ai_providers_block
    assert 'productRead("/api/v2/ai-providers/usage?days=7")' in ai_providers_block
    assert ".innerHTML" not in ai_providers_block

    #: Asset version bumped from the prior packet's pin.
    assert "20260827-approvals-autorefresh" in html
    assert "20260829-assistant-model-and-usage" not in html
    assert "sessionStorage" not in ai_providers_block

    #: Section-activation load, not a global poll timer (same convention as
    #: infrastructure/engineering/jobs panels).
    assert "refreshAiProvidersStatus();" in javascript
    assert "setInterval" not in ai_providers_block


def test_workspace_overview_consolidation_ports_health_activity_audit_and_administration():
    """DG-UI-UNIFICATION v1 U8: 總覽整併——worker 健康卡（GET
    /api/v2/servers）、活動與稽核合併 feed（新 GET /api/v2/events +
    /api/v2/audit 薄封裝）、管理入口（links only，非重製
    renderAdministrationSummary()）。等價保護：這是 U8 刪除
    `static/index.html`/`ui.js` 之前，總覽整併必須先落地的新面板 pin。

    Part A（總覽儀表板改版）擴充同一個 pin：總覽第一眼＝所有伺服器使用狀態
    （CPU/GPU/記憶體量表）＋專案數＋待核准數；其他內容預設收合，點開才看。
    """

    html = WORKSPACE_HTML.read_text(encoding="utf-8")
    javascript = WORKSPACE_JS.read_text(encoding="utf-8")

    #: markup
    assert 'id="overview-server-cards" class="card-list"' in html
    assert 'id="overview-servers-state"' in html
    assert 'id="overview-activity-list" class="card-list"' in html
    assert 'id="overview-activity-state"' in html
    assert 'id="overview-administration-links" class="button-row"' in html

    #: Part A Tier 3: 能力狀態／身分與授權／活動與稽核／管理 each collapsed
    #: by default (`<details class="panel">`, no `open` attribute) -- "其他
    #: 內容預設收合，點開才看".
    assert '<details class="panel">' in html
    assert '<details class="panel" id="overview-activity-panel">' in html
    assert '<details class="panel" open' not in html

    #: reused v2 read wrappers, no new fetch-boundary path.
    assert '"/api/v2/events"' in javascript
    assert '"/api/v2/audit"' in javascript
    assert '"/api/v2/legacy-projects"' in javascript
    assert '"/api/v2/approvals"' in javascript

    overview_block = javascript[
        javascript.index("function overviewServerBadge(") : javascript.index(
            "function emptyState(title, message)"
        )
    ]

    #: worker health card fields ported from legacy `renderServers()`
    #: (`static/index.html` :2734-2767): online badge, GPU util, VRAM,
    #: load1, disk 餘量, 目前任務 (cross-referenced against `/api/v2/jobs`,
    #: matching legacy's `jobsCache`), cached datasets.
    assert "productRead(\"/api/v2/servers\")" in overview_block
    assert "productRead(\"/api/v2/jobs\")" in overview_block
    assert "GPU 使用率" in overview_block
    assert "VRAM：" in overview_block
    assert "load1：" in overview_block
    assert "window.WorkspaceUI.formatGiB(serverState.disk_avail_bytes)" in overview_block
    assert "目前任務：" in overview_block
    assert "serverState.cached_datasets" in overview_block

    #: Part A Tier 2 (主視覺)：CPU/記憶體/GPU 量表 -- `.meter`/`.meter-fill`
    #: track+fill (see `workspace.css`), CPU label string, and
    #: `mem_total_bytes` driving the 記憶體 meter (cap-at-100%/missing-field
    #: fallbacks stay honest, never a fabricated percentage).
    assert '"meter-row"' in overview_block
    assert '"meter"' in overview_block
    assert '"meter-fill"' in overview_block
    assert "CPU 負載（load1/核心）" in overview_block
    assert "serverState.mem_total_bytes" in overview_block
    assert "serverState.mem_available_bytes" in overview_block
    assert "serverState.cpu_count" in overview_block

    #: Part A: 30s poll while 總覽 is the active section AND the tab is
    #: visible -- single cancellable timer (serial-bump pattern, same as
    #: `agentSessionStopPolling()`/`chatStop()`), stopped on section leave /
    #: `visibilitychange` hidden / `clearWorkspace()`.
    assert "state.overviewPollSerial += 1" in overview_block
    assert "clearTimeout(state.overviewPollTimer)" in overview_block
    assert 'document.visibilityState === "visible"' in overview_block
    assert "OVERVIEW_SERVERS_POLL_INTERVAL_MS" in overview_block

    #: activity/audit merged feed: full JSON stays collapsed behind
    #: 查看原始內容 (evidence never dropped), never innerHTML.
    assert 'productRead("/api/v2/events?limit=50")' in overview_block
    assert '"查看原始內容"' in overview_block
    assert "JSON.stringify(record, null, 2)" in overview_block
    assert ".innerHTML" not in overview_block

    #: Part A: 活動與稽核 is lazy -- first `toggle`-open triggers
    #: `loadOverviewActivity()`, not the eager `initialize()` path.
    assert 'element("overview-activity-panel").addEventListener("toggle"' in javascript
    assert "!state.overviewActivityLoaded" in javascript

    #: administration: links + deferral note only, no re-implementation of
    #: legacy `renderAdministrationSummary()`'s membership-project fetch.
    assert "function renderOverviewAdministrationLinks()" in overview_block
    assert 'activateSection("projects")' in overview_block

    #: Part A Tier 1: 專案數／待核准數 -- accurate count from
    #: `/api/v2/legacy-projects`/`/api/v2/approvals?status=pending`, each
    #: card clickable to its section.
    assert "function renderOverviewSummary()" in javascript
    assert "async function loadOverviewSummary()" in javascript
    assert 'productRead("/api/v2/legacy-projects")' in javascript
    assert 'productRead("/api/v2/approvals?status=pending&limit=100")' in javascript
    assert 'activateSection("approvals")' in javascript

    #: once-on-`initialize()` load wiring (overview is the default-visible
    #: section, unlike jobs/infrastructure/projects/engineering which each
    #: load on their own `activateSection()` branch). 活動與稽核 is the one
    #: exception -- lazy on toggle, not eager here (see above).
    assert "loadOverviewServers();" in javascript
    assert "loadOverviewSummary();" in javascript
    assert "loadOverviewActivity();" in javascript
    assert "renderOverviewAdministrationLinks();" in javascript
    assert 'element("overview-server-cards").replaceChildren();' in javascript

    #: Approvals auto-refresh (no full-page reload needed to see a new
    #: pending card, a decided card leave, or the pending-count badge
    #: update): 15s poll of the same `GET /api/v2/approvals?status=pending`
    #: endpoint the overview summary already uses, same serial-bump/single-
    #: timer convention as `OVERVIEW_SERVERS_POLL_INTERVAL_MS` above, but not
    #: gated on which section is active (the overview badge must stay
    #: accurate no matter where the user currently is).
    assert "APPROVALS_POLL_INTERVAL_MS = 15000" in javascript
    assert "state.approvalsPollSerial += 1" in javascript
    assert "clearTimeout(state.approvalsPollTimer)" in javascript
    #: at most one in-flight poll request
    assert "state.approvalsPollInFlight" in javascript
    #: cheap change signature (sorted `id:status` + count) -- unchanged
    #: signature is a silent no-op, no DOM churn/scroll reset.
    assert "function approvalsListSignature(items)" in javascript
    assert "signature === state.approvalsListSignature" in javascript
    #: an open inline approval detail (half-filled decide/reject form) must
    #: never be clobbered by a background poll re-render -- the list
    #: re-render is deferred until the panel closes.
    assert "state.approvalsListRenderPending = true" in javascript
    assert "state.approvalsListRenderPending" in javascript
    #: pause while the tab itself is hidden, resume with an immediate
    #: refresh (not just a fresh 15s timer) when it becomes visible again.
    assert "approvalsStopPolling();" in javascript
    assert "approvalsStartPolling({ immediate: true });" in javascript


def test_engineering_task_retry_approval_visible_to_platform_admin_under_enforce(
    api_client,
):
    """Pilot bug fix (pending approval #166, `engineering_task_retry`): the
    kind had no classification in `resolve_approval_resource()`, so under
    `AUTHORIZATION_MODE=enforce` it fell through `_legacy_approval_target()`'s
    unresolved-kind fallback to opaque `ResourceScope.GLOBAL` handling before
    the platform-admin check, hiding the card from the v2 Workspace list.
    This asserts it is now visible in v2 list/detail, decidable through the
    generic v2 decision path, and stays visible on the legacy `/approvals`
    list (same underlying resolver, DG-UI-UNIFICATION v1 U1 fix)."""

    from app.approvals import request_engineering_task_retry_approval
    from tests.test_engineering_task_retry_discard import (
        _create_attempt_one,
        _mark_attempt_one_failed,
    )

    client, main_module = api_client
    _enable_v2(main_module)
    database = main_module.app_state.db
    admin = _create_human(database, platform_admin=True)

    ctx = _create_attempt_one(database)
    _mark_attempt_one_failed(database, ctx)
    database.update_engineering_task(ctx["task_id"], status="failed")
    approval = request_engineering_task_retry_approval(
        database,
        ctx["task_id"],
        audit_path=main_module.app_state.config.audit_path,
    )

    _session_for(client, main_module, admin.id)

    listed = client.get("/api/v2/approvals?kind=engineering_task_retry").json()
    assert [item["id"] for item in listed["items"]] == [approval.id]
    assert listed["items"][0]["project_id"] == ctx["project_id"]

    detail = client.get(f"/api/v2/approvals/{approval.id}").json()
    assert detail["kind"] == "engineering_task_retry"
    assert detail["can_decide"] is True

    legacy_listed = client.get("/approvals?kind=engineering_task_retry").json()
    assert [item["id"] for item in legacy_listed] == [approval.id]

    decision_url = f"/api/v2/approvals/{approval.id}/decisions"
    decided = client.post(
        decision_url,
        json={"decision": "reject", "note": "visibility coverage only"},
        headers={
            "Idempotency-Key": "engineering-task-retry-visibility",
            "X-Approval-Payload-Digest": detail["payload_digest"],
        },
    )
    assert decided.status_code == 202
    assert decided.json()["status"] == "rejected"
    assert database.get_approval(approval.id).status == "rejected"


def test_run_profile_create_approval_visible_to_scoped_project_role_under_enforce(
    api_client,
):
    """Companion coverage for a project-scoped (not platform-admin) kind:
    `run_profile_create` had the same U1 classification gap. A project OWNER
    (no `platform_admin`) must see and be able to decide it once
    `resolve_approval_resource()` resolves it to that project's scope."""

    from app.approvals import request_run_profile_create_approval

    client, main_module = api_client
    _enable_v2(main_module)
    main_module.app_state.config.run_profile_v1_enabled = True
    database = main_module.app_state.db
    owner = _create_human(database, name="Owner")
    project_id = database.insert_project("run-profile-vis", "/private/run-profile-vis")
    _insert_binding(
        database,
        project_id=project_id,
        actor_id=owner.id,
        role=ProjectRoleV2.OWNER,
    )
    approval = request_run_profile_create_approval(
        database,
        "run-profile-vis",
        "default",
        command="python train.py",
        config=main_module.app_state.config,
        audit_path=main_module.app_state.config.audit_path,
    )

    _session_for(client, main_module, owner.id)

    listed = client.get("/api/v2/approvals?kind=run_profile_create").json()
    assert [item["id"] for item in listed["items"]] == [approval.id]
    assert listed["items"][0]["project_id"] == project_id

    detail = client.get(f"/api/v2/approvals/{approval.id}").json()
    assert detail["kind"] == "run_profile_create"
    assert detail["can_decide"] is True

    legacy_listed = client.get("/approvals?kind=run_profile_create").json()
    assert [item["id"] for item in legacy_listed] == [approval.id]
