"""PR-03 Product v2 identity, sessions, workspace, and shell contracts."""

from __future__ import annotations

import pytest

import json
from pathlib import Path

from app.authentication import ensure_legacy_admin_actor
from app.approval_presentation import describe_approval
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


@pytest.mark.usefixtures("legacy_posture")
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


def test_v2_root_serves_login_page_when_unauthenticated_and_studio_once_signed_in(
    api_client,
):
    """Login-first root, DG-STUDIO-UI v1 P3-4 (root cutover): an
    unauthenticated `GET /` serves `static/login.html`; a resolved
    session/service/legacy credential -- the exact same
    `resolve_request_context()` helper `auth_middleware` uses -- serves the
    Studio SPA build (`static/studio/index.html`), or the inline build-me
    notice when the gitignored build is absent. The retired v2 Workspace
    shell no longer exists in either state. `GET /` remains exempt from the
    401 gate either way (INV-APPROVAL-5): every variant returns 200."""

    client, main_module = api_client
    main_module.app_state.config.api_v2_enabled = True

    anonymous_root = client.get("/")
    anonymous = client.get("/api/v2/me")

    assert anonymous_root.status_code == 200
    assert anonymous_root.headers["Cache-Control"] == "no-store"
    assert "使用 OIDC 登入" in anonymous_root.text
    assert '<div id="root"></div>' not in anonymous_root.text
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authentication_required"
    assert anonymous.headers["Cache-Control"] == "no-store"
    assert anonymous.headers["X-OIDC-Enabled"] == "false"

    _create_human(main_module.app_state.db)
    _session_for(client, main_module, ACTOR_ID)
    v2_root = client.get("/")

    assert v2_root.status_code == 200
    assert v2_root.headers["Cache-Control"] == "no-store"
    assert 'id="workspace-navigation"' not in v2_root.text
    # Studio build when present (local/CI builds land it), the honest inline
    # notice otherwise -- never a 404, never the retired Workspace shell.
    assert ('<div id="root"></div>' in v2_root.text) or ("Studio 尚未建置" in v2_root.text)

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


#: 整頓 C6 follow-up: under the all-on defaults this projection lists no
#: projects for a platform admin without role bindings (flag interplay, not
#: a single gate); the Studio does not consume /api/v2/workspace, so the
#: legacy baseline keeps the test's original intent until it is characterized.
@pytest.mark.usefixtures("legacy_posture")
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


#: 整頓 C6 follow-up: under the all-on defaults this projection lists no
#: projects for a platform admin without role bindings (flag interplay, not
#: a single gate); the Studio does not consume /api/v2/workspace, so the
#: legacy baseline keeps the test's original intent until it is characterized.
@pytest.mark.usefixtures("legacy_posture")
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
        "title": "排入任務",
        "summary": describe_approval("enqueue", payload)["summary"],
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
