from app.authentication import ensure_legacy_admin_actor
from app.identity import ActorType, ProjectRole, generate_service_token, generate_session_token


def _session_for(main_module, client, actor_id: str) -> None:
    issued = generate_session_token()
    main_module.app_state.db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor_id,
        secret_hash=issued.secret_hash,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    client.cookies.set(main_module.app_state.config.session_cookie_name, issued.raw_token)


def test_enforce_filters_project_and_job_collections_and_denies_cross_project(
    api_client,
):
    client, main_module = api_client
    db = main_module.app_state.db
    actor = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Viewer")
    project_a = db.insert_project("enforce-a", "/srv/enforce-a")
    db.insert_project("enforce-b", "/srv/enforce-b")
    db.upsert_project_membership(
        project=project_a,
        actor_id=actor.id,
        role=ProjectRole.VIEWER,
    )
    db.insert_job(command="a", project="enforce-a")
    db.insert_job(command="b", project="enforce-b")
    _session_for(main_module, client, actor.id)
    main_module.app_state.config.authorization_mode = "enforce"

    projects = client.get("/projects")
    assert projects.status_code == 200
    assert [item["name"] for item in projects.json()] == ["enforce-a"]

    jobs = client.get("/jobs")
    assert jobs.status_code == 200
    assert [item["project"] for item in jobs.json()] == ["enforce-a"]

    denied = client.get("/projects/enforce-b/detail")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "authorization_denied"
    assert denied.json()["error"]["details"]["reason"] == "denied_cross_project"


def test_enforce_requires_authentication_and_service_scope(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    project = db.insert_project("enforce-auth", "/srv/enforce-auth")
    main_module.app_state.config.authorization_mode = "enforce"

    anonymous = client.get(f"/projects/{project}/detail")
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "authorization_required"
    assert anonymous.headers["X-Request-ID"] == anonymous.json()["error"]["request_id"]

    service = db.insert_actor(
        actor_type=ActorType.SERVICE,
        display_name="Scoped service",
    )
    db.insert_service_account(actor_id=service.id, name="scoped-service")
    db.upsert_project_membership(
        project=project,
        actor_id=service.id,
        role=ProjectRole.OPERATOR,
    )
    issued = generate_service_token()
    db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=service.id,
        secret_hash=issued.secret_hash,
        scopes=["project.operate"],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.service_token_auth_enabled = True

    denied = client.get(
        f"/projects/{project}/detail",
        headers={"Authorization": f"Bearer {issued.raw_token}"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["reason"] == "denied_service_scope_missing"


def test_enforce_legacy_shared_token_is_not_global_admin(api_client):
    client, main_module = api_client
    ensure_legacy_admin_actor(main_module.app_state.db)
    main_module.app_state.config.auth_token = "legacy-enforce-secret"
    main_module.app_state.config.authorization_mode = "enforce"

    denied = client.get(
        "/servers",
        headers={"X-Auth-Token": "legacy-enforce-secret"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["reason"] == "denied_legacy_shared_token"

    self_view = client.get(
        "/auth/me",
        headers={"X-Auth-Token": "legacy-enforce-secret"},
    )
    assert self_view.status_code == 200


def test_enforce_blocks_high_risk_self_approval(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    actor = db.insert_actor(actor_type=ActorType.HUMAN, display_name="Admin")
    project = db.insert_project("enforce-approval", "/srv/enforce-approval")
    db.upsert_project_membership(
        project=project,
        actor_id=actor.id,
        role=ProjectRole.ADMIN,
    )
    approval_id = db.insert_approval(
        "project_membership_upsert",
        {"project_id": db.get_project(project).id, "actor_id": actor.id, "role": "viewer"},
        requester_actor_id=actor.id,
    )
    _session_for(main_module, client, actor.id)
    main_module.app_state.config.authorization_mode = "enforce"

    denied = client.post(f"/approve/{approval_id}")
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["reason"] == "denied_high_risk_self_decision"


def test_enforce_preserves_disabled_feature_route_404(api_client):
    client, main_module = api_client
    main_module.app_state.config.authorization_mode = "enforce"
    main_module.app_state.config.node_agent_v1_enabled = False
    main_module.app_state.config.node_protocol_drain_enabled = False
    main_module.app_state.config.identity_admin_enabled = False
    main_module.app_state.config.run_profile_v1_enabled = False
    main_module.app_state.config.dispatch_policy_v1_enabled = False
    main_module.app_state.config.server_bootstrap_v1_enabled = False

    for path in (
        "/nodes",
        "/identity/service-accounts",
        "/projects/example/run-profiles",
        "/projects/example/dispatch-policies",
        "/servers/bootstrap-reports",
    ):
        response = client.get(path)
        assert response.status_code == 404, path


def test_enforce_applies_service_scopes_inside_local_agent_tools(api_client):
    client, main_module = api_client
    db = main_module.app_state.db
    service = db.insert_actor(actor_type=ActorType.SERVICE, display_name="Agent")
    db.insert_service_account(actor_id=service.id, name="agent")
    issued = generate_service_token()
    db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=service.id,
        secret_hash=issued.secret_hash,
        scopes=["identity.self.view"],
        expires_at="2099-01-01T00:00:00+00:00",
    )
    main_module.app_state.config.authorization_mode = "enforce"
    main_module.app_state.config.service_token_auth_enabled = True

    denied = client.post(
        "/agent/cmd",
        json={"cmd": "servers"},
        headers={"Authorization": f"Bearer {issued.raw_token}"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["action"] == "platform.view"
    assert denied.json()["error"]["details"]["reason"] == "denied_service_scope_missing"


def test_enforce_keeps_static_mount_public(api_client):
    client, main_module = api_client
    main_module.app_state.config.authorization_mode = "enforce"

    response = client.get("/static/index.html")

    assert response.status_code == 200
