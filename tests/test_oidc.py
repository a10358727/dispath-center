"""Slice 7 OIDC lifecycle, session, replay, and compatibility security tests."""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.audit import read_audit
from app.authentication import ensure_legacy_admin_actor
from app.config import AppConfig
from app.identity import hash_secret, parse_session_token, verify_secret
from app.oidc import (
    OIDCClaims,
    OIDCProviderError,
    OIDCValidationError,
    pkce_s256_challenge,
)


ISSUER = "https://identity.example.test"
CLIENT_ID = "dispatch-center"
CLIENT_SECRET = "test-client-secret-never-log"
REDIRECT_URI = "https://dispatch.example.test/auth/callback"
WS_URI = "wss://dispatch.example.test/ws"
FLOW_COOKIE = "dispatch_oidc_flow"
SESSION_COOKIE = "dispatch_session"


class FakeOIDCProvider:
    """No-network provider implementing the injected production protocol."""

    def __init__(self) -> None:
        self.authorization_calls: list[dict] = []
        self.exchange_calls: list[dict] = []
        self.subject = "human-subject-1"
        self.display_name = "Ada Operator"
        self.email = "ada@example.test"
        self.claim_overrides: dict = {}
        self.authorization_error: Exception | None = None
        self.exchange_error: Exception | None = None

    async def authorization_url(self, **kwargs) -> str:
        self.authorization_calls.append(dict(kwargs))
        if self.authorization_error is not None:
            raise self.authorization_error
        return "https://identity.example.test/authorize?" + urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": kwargs["redirect_uri"],
                "state": kwargs["state"],
                "nonce": kwargs["nonce"],
                "code_challenge": kwargs["code_challenge"],
                "code_challenge_method": "S256",
            }
        )

    async def exchange_code(self, **kwargs) -> OIDCClaims:
        self.exchange_calls.append(dict(kwargs))
        if self.exchange_error is not None:
            raise self.exchange_error
        claims = OIDCClaims(
            issuer=ISSUER,
            subject=self.subject,
            audience=(CLIENT_ID,),
            expires_at=time.time() + 600,
            nonce=kwargs["nonce"],
            display_name=self.display_name,
            email=self.email,
        )
        return replace(claims, **self.claim_overrides)


@pytest.fixture
def oidc_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "oidc.db"))
    monkeypatch.setenv("AUDIT_PATH", str(tmp_path / "oidc-audit.jsonl"))
    monkeypatch.setenv("SERVERS_YAML_PATH", str(tmp_path / "servers.yaml"))
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("OIDC_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("OIDC_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setenv("OIDC_PLATFORM_ADMIN_SUBJECTS", "")
    monkeypatch.setenv("VLLM_BASE_URL", "")
    monkeypatch.setenv("VLLM_MODEL", "")
    monkeypatch.delenv("AUTH_TOKEN", raising=False)

    import app.main as main_module

    fake = FakeOIDCProvider()
    with TestClient(
        main_module.app,
        base_url="https://dispatch.example.test",
    ) as client:
        main_module.app_state.oidc_provider = fake
        yield client, main_module, fake


def _begin_login(client, fake, return_to="/"):
    response = client.get(
        "/auth/login",
        params={"return_to": return_to},
        follow_redirects=False,
    )
    assert response.status_code == 302
    call = fake.authorization_calls[-1]
    return response, call


def _finish_login(client, raw_state, code="one-time-code"):
    return client.get(
        "/auth/callback",
        params={"state": raw_state, "code": code},
        follow_redirects=False,
    )


def _database_text_values(db):
    tables = [
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        for row in db._conn.execute(f'SELECT * FROM "{table}"').fetchall():
            for value in row:
                if isinstance(value, str):
                    yield value


def test_oidc_login_callback_issues_hashed_revocable_secure_session(oidc_client):
    client, main_module, fake = oidc_client
    before_me = client.get("/auth/me")
    assert before_me.status_code == 401
    assert before_me.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}
    assert before_me.headers["X-OIDC-Enabled"] == "true"
    assert before_me.headers["Cache-Control"] == "no-store"

    login, auth_call = _begin_login(
        client,
        fake,
        return_to="/#project/example?tab=activity",
    )
    raw_state = auth_call["state"]
    raw_nonce = auth_call["nonce"]
    flow_cookie = login.headers.get_list("set-cookie")
    assert len(flow_cookie) == 1
    assert FLOW_COOKIE in flow_cookie[0]
    assert "HttpOnly" in flow_cookie[0]
    assert "Secure" in flow_cookie[0]
    assert "SameSite=lax" in flow_cookie[0]
    assert "Path=/auth/callback" in flow_cookie[0]
    assert "code_challenge_method=S256" in login.headers["location"]
    assert "code_verifier" not in login.headers["location"]

    flow = main_module.app_state.db.get_oidc_login_flow(hash_secret(raw_state))
    assert flow is not None
    assert flow.state_hash == hash_secret(raw_state)
    assert flow.nonce_hash == hash_secret(raw_nonce)
    assert flow.pkce_verifier
    assert auth_call["code_challenge"] == pkce_s256_challenge(flow.pkce_verifier)
    persisted_before = list(_database_text_values(main_module.app_state.db))
    assert raw_state not in persisted_before
    assert raw_nonce not in persisted_before

    callback = _finish_login(client, raw_state)

    assert callback.status_code == 303
    assert callback.headers["location"] == "/#project/example?tab=activity"
    set_cookies = callback.headers.get_list("set-cookie")
    session_cookie = next(value for value in set_cookies if SESSION_COOKIE in value)
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "Max-Age=28800" in session_cookie
    assert any(FLOW_COOKIE in value and "Max-Age=0" in value for value in set_cookies)
    assert callback.headers["Cache-Control"] == "no-store"

    exchange = fake.exchange_calls[-1]
    assert exchange["code"] == "one-time-code"
    assert exchange["code_verifier"] == flow.pkce_verifier
    assert exchange["nonce"] == raw_nonce
    assert exchange["redirect_uri"] == REDIRECT_URI
    consumed = main_module.app_state.db.get_oidc_login_flow(hash_secret(raw_state))
    assert consumed.consumed_at is not None
    assert consumed.pkce_verifier is None

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.headers["Cache-Control"] == "no-store"
    assert me.json() == {
        "authenticated": True,
        "authentication_method": "session",
        "oidc_enabled": True,
        "actor": {
            "id": me.json()["actor"]["id"],
            "type": "human",
            "display_name": "Ada Operator",
            "platform_admin": False,
        },
        "project_memberships": [],
        "service_scopes": [],
    }
    identity = main_module.app_state.db.get_oidc_identity_by_subject(
        ISSUER, fake.subject
    )
    assert identity.actor_id == me.json()["actor"]["id"]
    assert identity.email == fake.email

    raw_session = client.cookies.get(SESSION_COOKIE)
    session_id, raw_session_secret = parse_session_token(raw_session)
    session = main_module.app_state.db.get_actor_session(session_id)
    assert verify_secret(raw_session, session.secret_hash)
    persisted_after = list(_database_text_values(main_module.app_state.db))
    assert raw_session not in persisted_after
    assert raw_session_secret not in persisted_after
    assert "one-time-code" not in persisted_after


def test_logout_revokes_only_verified_presented_session_and_clears_cookie(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    assert _finish_login(client, call["state"]).status_code == 303
    raw_session = client.cookies.get(SESSION_COOKIE)
    session_id, _ = parse_session_token(raw_session)

    response = client.post("/auth/logout")

    assert response.status_code == 204
    assert response.headers["Cache-Control"] == "no-store"
    assert "Max-Age=0" in response.headers.get_list("set-cookie")[0]
    assert main_module.app_state.db.get_actor_session(session_id).revoked_at is not None
    assert client.get("/auth/me").status_code == 401
    assert read_audit(main_module.app_state.config.audit_path)[-1]["action"] == "oidc_logout"


def test_logout_cannot_revoke_by_session_uuid_without_matching_secret(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    assert _finish_login(client, call["state"]).status_code == 303
    raw_session = client.cookies.get(SESSION_COOKIE)
    session_id, _ = parse_session_token(raw_session)

    # Authenticate the logout request through the compatible legacy header so
    # the forged cookie reaches the route; UUID knowledge alone must not revoke
    # the real server-side session.
    main_module.app_state.config.auth_token = "legacy-compatible-token"
    ensure_legacy_admin_actor(main_module.app_state.db)
    client.cookies.clear()
    client.cookies.set(
        SESSION_COOKIE,
        f"dcsess_{session_id}.forged-secret",
        path="/",
    )

    response = client.post(
        "/auth/logout", headers={"X-Auth-Token": "legacy-compatible-token"}
    )

    assert response.status_code == 204
    assert main_module.app_state.db.get_actor_session(session_id).revoked_at is None
    assert read_audit(main_module.app_state.config.audit_path)[-1]["params"] == {
        "session_revoked": False
    }


@pytest.mark.parametrize(
    "return_to",
    [
        "https://evil.example/",
        "//evil.example/path",
        "///evil.example/path",
        "/\\evil.example/path",
        "relative/path",
        "javascript:alert(1)",
        "/safe\r\nX-Injected: yes",
    ],
)
def test_login_rejects_non_relative_or_unsafe_return_to(oidc_client, return_to):
    client, main_module, fake = oidc_client
    response = client.get(
        "/auth/login", params={"return_to": return_to}, follow_redirects=False
    )
    assert response.status_code == 400
    assert fake.authorization_calls == []
    assert main_module.app_state.db._conn.execute(
        "SELECT COUNT(*) FROM oidc_login_flows"
    ).fetchone()[0] == 0


def test_login_rejects_duplicate_and_percent_encoded_network_return_to(oidc_client):
    client, main_module, fake = oidc_client

    duplicate = client.get(
        "/auth/login?return_to=%2Fsafe&return_to=%2Fother",
        follow_redirects=False,
    )
    encoded_network_path = client.get(
        "/auth/login?return_to=%2F%2Fevil.example%2Fpath",
        follow_redirects=False,
    )
    encoded_backslash = client.get(
        "/auth/login?return_to=%2F%5Cevil.example%2Fpath",
        follow_redirects=False,
    )

    assert duplicate.status_code == 400
    assert encoded_network_path.status_code == 400
    assert encoded_backslash.status_code == 400
    assert fake.authorization_calls == []
    assert main_module.app_state.db._conn.execute(
        "SELECT COUNT(*) FROM oidc_login_flows"
    ).fetchone()[0] == 0


def test_callback_requires_browser_bound_cookie_before_consuming_state(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    raw_state = call["state"]
    client.cookies.clear()

    response = _finish_login(client, raw_state)

    assert response.status_code == 400
    assert fake.exchange_calls == []
    flow = main_module.app_state.db.get_oidc_login_flow(hash_secret(raw_state))
    assert flow.consumed_at is None
    assert flow.pkce_verifier is not None


def test_callback_state_mismatch_cannot_burn_another_browser_flow(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    raw_state = call["state"]

    response = _finish_login(client, raw_state + "x")

    assert response.status_code == 400
    assert fake.exchange_calls == []
    assert main_module.app_state.db.get_oidc_login_flow(
        hash_secret(raw_state)
    ).consumed_at is None


def test_duplicate_state_is_rejected_without_consuming_flow(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    raw_state = call["state"]

    response = client.get(
        "/auth/callback",
        params=[
            ("state", raw_state),
            ("state", raw_state),
            ("code", "one-time-code"),
        ],
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert fake.exchange_calls == []
    assert main_module.app_state.db.get_oidc_login_flow(
        hash_secret(raw_state)
    ).consumed_at is None


def test_duplicate_code_and_tampered_nonce_hash_fail_before_exchange(oidc_client):
    client, main_module, fake = oidc_client
    _, duplicate = _begin_login(client, fake)
    duplicate_response = client.get(
        "/auth/callback",
        params=[
            ("state", duplicate["state"]),
            ("code", "first-code"),
            ("code", "second-code"),
        ],
        follow_redirects=False,
    )
    assert duplicate_response.status_code == 400
    assert fake.exchange_calls == []
    assert main_module.app_state.db.get_oidc_login_flow(
        hash_secret(duplicate["state"])
    ).consumed_at is not None

    _, tampered = _begin_login(client, fake)
    main_module.app_state.db._conn.execute(
        "UPDATE oidc_login_flows SET nonce_hash = ? WHERE state_hash = ?",
        (hash_secret("different-nonce"), hash_secret(tampered["state"])),
    )
    main_module.app_state.db._conn.commit()
    tampered_response = _finish_login(client, tampered["state"])

    assert tampered_response.status_code == 400
    assert fake.exchange_calls == []


def test_consumed_callback_is_replay_protected_before_provider_exchange(oidc_client):
    client, _main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    raw_state = call["state"]
    assert _finish_login(client, raw_state).status_code == 303
    assert len(fake.exchange_calls) == 1
    client.cookies.set(FLOW_COOKIE, raw_state, path="/auth/callback")

    replay = _finish_login(client, raw_state, code="replayed-code")

    assert replay.status_code == 400
    assert len(fake.exchange_calls) == 1


def test_expired_flow_is_rejected_and_never_exchanged(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    raw_state = call["state"]
    main_module.app_state.db._conn.execute(
        "UPDATE oidc_login_flows SET expires_at = ? WHERE state_hash = ?",
        ("2000-01-01T00:00:00+00:00", hash_secret(raw_state)),
    )
    main_module.app_state.db._conn.commit()

    response = _finish_login(client, raw_state)

    assert response.status_code == 400
    assert fake.exchange_calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"issuer": "https://wrong-issuer.example"},
        {"audience": ("wrong-client",)},
        {"audience": (CLIENT_ID, 7)},
        {"expires_at": 1},
        {"nonce": "wrong-nonce"},
        {"subject": ""},
    ],
)
def test_defensive_claim_checks_reject_invalid_provider_output(oidc_client, overrides):
    client, main_module, fake = oidc_client
    fake.claim_overrides = overrides
    _, call = _begin_login(client, fake)

    response = _finish_login(client, call["state"])

    assert response.status_code == 400
    assert client.get("/auth/me").status_code == 401
    assert main_module.app_state.db.list_actors(actor_type="human") == []


def test_provider_validation_failure_is_generic_and_consumes_flow(oidc_client):
    client, main_module, fake = oidc_client
    fake.exchange_error = OIDCValidationError()
    _, call = _begin_login(client, fake)

    response = _finish_login(client, call["state"], code="sensitive-code")

    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC provider response was invalid"}
    assert "sensitive-code" not in response.text
    assert main_module.app_state.db.get_oidc_login_flow(
        hash_secret(call["state"])
    ).consumed_at is not None


def test_binding_uses_issuer_subject_not_email_and_refreshes_metadata(oidc_client):
    client, main_module, fake = oidc_client
    fake.subject = "subject-a"
    fake.email = "shared@example.test"
    _, first = _begin_login(client, fake)
    assert _finish_login(client, first["state"]).status_code == 303
    actor_a = client.get("/auth/me").json()["actor"]["id"]
    assert client.post("/auth/logout").status_code == 204

    fake.subject = "subject-b"
    fake.display_name = "Different Subject"
    _, second = _begin_login(client, fake)
    assert _finish_login(client, second["state"]).status_code == 303
    actor_b = client.get("/auth/me").json()["actor"]["id"]
    assert actor_b != actor_a
    assert client.post("/auth/logout").status_code == 204

    fake.subject = "subject-a"
    fake.display_name = "Ada Renamed"
    fake.email = "changed@example.test"
    _, third = _begin_login(client, fake)
    assert _finish_login(client, third["state"]).status_code == 303
    me = client.get("/auth/me").json()
    assert me["actor"]["id"] == actor_a
    assert me["actor"]["display_name"] == "Ada Renamed"
    assert main_module.app_state.db.get_actor(actor_a).email == "changed@example.test"
    assert len(main_module.app_state.db.list_actors(actor_type="human")) == 2


def test_platform_admin_bootstrap_is_exact_subject_and_create_only(oidc_client):
    client, main_module, fake = oidc_client
    fake.subject = "ordinary-subject"
    _, first = _begin_login(client, fake)
    assert _finish_login(client, first["state"]).status_code == 303
    ordinary_actor_id = client.get("/auth/me").json()["actor"]["id"]
    assert main_module.app_state.db.get_actor(ordinary_actor_id).platform_admin is False
    assert client.post("/auth/logout").status_code == 204

    # Changing login config later must not elevate an already-bound actor.
    main_module.app_state.config.oidc_platform_admin_subjects = frozenset(
        {"ordinary-subject", "configured-admin"}
    )
    _, second = _begin_login(client, fake)
    assert _finish_login(client, second["state"]).status_code == 303
    assert main_module.app_state.db.get_actor(ordinary_actor_id).platform_admin is False
    assert client.post("/auth/logout").status_code == 204

    fake.subject = "configured-admin"
    _, third = _begin_login(client, fake)
    assert _finish_login(client, third["state"]).status_code == 303
    admin = client.get("/auth/me").json()["actor"]
    assert admin["id"] != ordinary_actor_id
    assert admin["platform_admin"] is True


def test_disabled_bound_actor_cannot_receive_a_new_session(oidc_client):
    client, main_module, fake = oidc_client
    _, first = _begin_login(client, fake)
    assert _finish_login(client, first["state"]).status_code == 303
    actor_id = client.get("/auth/me").json()["actor"]["id"]
    assert client.post("/auth/logout").status_code == 204
    main_module.app_state.db.update_actor(
        actor_id, disabled_at="2026-07-13T00:00:00+00:00"
    )
    _, second = _begin_login(client, fake)

    response = _finish_login(client, second["state"])

    assert response.status_code == 403
    assert client.get("/auth/me").status_code == 401


def test_only_exact_get_handshake_routes_are_unauthenticated(oidc_client):
    client, _main_module, fake = oidc_client
    assert client.get("/auth/login", follow_redirects=False).status_code == 302
    assert fake.authorization_calls
    assert client.get("/auth/callback", follow_redirects=False).status_code == 400
    for method, path in [
        ("post", "/auth/login"),
        ("head", "/auth/login"),
        ("post", "/auth/callback"),
        ("head", "/auth/callback"),
        ("get", "/auth/me"),
        ("post", "/auth/logout"),
        ("get", "/servers"),
    ]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 401, (method, path, response.text)
        if method != "head":
            assert response.json() == {"detail": "缺少或錯誤的 X-Auth-Token"}


def test_return_to_validator_rejects_raw_browser_network_paths():
    from app.main import _validate_oidc_return_to

    for value in ("//evil.example/path", "///evil.example/path", "////evil/path"):
        with pytest.raises(ValueError, match="invalid return_to"):
            _validate_oidc_return_to(value)
    assert _validate_oidc_return_to("/#project/safe") == "/#project/safe"


def test_oidc_only_websocket_requires_session_and_session_skips_auth_envelope(
    oidc_client, monkeypatch
):
    client, main_module, fake = oidc_client

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(WS_URI) as websocket:
            websocket.receive_json()
    assert exc_info.value.code == 1008

    _, call = _begin_login(client, fake)
    assert _finish_login(client, call["state"]).status_code == 303
    captured = {}

    async def fake_handle_chat_text(text, **kwargs):
        captured["text"] = text
        captured["context"] = kwargs["request_context"]
        return [{"type": "reply", "text": "session-ok"}]

    monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
    with client.websocket_connect(WS_URI) as websocket:
        websocket.send_json({"type": "chat", "text": "first-chat"})
        assert websocket.receive_json() == {"type": "reply", "text": "session-ok"}

    assert captured["text"] == "first-chat"
    assert captured["context"].authentication_method == "session"


def test_revoked_oidc_session_stops_existing_websocket_before_next_chat(
    oidc_client, monkeypatch
):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    assert _finish_login(client, call["state"]).status_code == 303
    session_id, _ = parse_session_token(client.cookies.get(SESSION_COOKIE))
    handled = []

    async def fake_handle_chat_text(text, **kwargs):
        handled.append(text)
        return [{"type": "reply", "text": "must-not-run"}]

    monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(WS_URI) as websocket:
            assert main_module.app_state.db.revoke_actor_session(session_id) is True
            websocket.send_json({"type": "chat", "text": "after-revocation"})
            websocket.receive_json()

    assert exc_info.value.code == 1008
    assert handled == []


def test_oidc_enabled_preserves_legacy_http_and_websocket_protocol(
    oidc_client, monkeypatch
):
    client, main_module, _fake = oidc_client
    main_module.app_state.config.auth_token = "legacy-compatible-token"
    legacy_actor = ensure_legacy_admin_actor(main_module.app_state.db)

    me = client.get(
        "/auth/me", headers={"X-Auth-Token": "legacy-compatible-token"}
    )
    assert me.status_code == 200
    assert me.json()["actor"]["id"] == legacy_actor.id
    assert me.json()["authentication_method"] == "legacy_shared_token"

    captured = {}

    async def fake_handle_chat_text(text, **kwargs):
        captured["context"] = kwargs["request_context"]
        return [{"type": "reply", "text": "legacy-ok"}]

    monkeypatch.setattr(main_module, "handle_chat_text", fake_handle_chat_text)
    with client.websocket_connect(WS_URI) as websocket:
        websocket.send_json({"type": "auth", "token": "legacy-compatible-token"})
        websocket.send_json({"type": "chat", "text": "legacy-chat"})
        assert websocket.receive_json() == {"type": "reply", "text": "legacy-ok"}

    assert captured["context"].authentication_method == "legacy_shared_token"


def test_oidc_session_remains_observational_in_authorization_shadow_mode(oidc_client):
    client, main_module, fake = oidc_client
    _, call = _begin_login(client, fake)
    assert _finish_login(client, call["state"]).status_code == 303
    actor_id = client.get("/auth/me").json()["actor"]["id"]
    main_module.app_state.config.authorization_mode = "shadow"

    response = client.get("/servers")

    assert response.status_code == 200
    assert response.json() == []
    record = read_audit(main_module.app_state.config.audit_path)[-1]
    assert record["action"] == "authorization_shadow_denied"
    assert record["result"] == "would_deny"
    assert record["actor"]["id"] == actor_id
    assert record["params"]["action"] == "platform.view"


def test_oidc_disabled_routes_do_not_construct_or_contact_provider(api_client):
    client, main_module = api_client
    assert main_module.app_state.config.oidc_enabled is False
    assert main_module.app_state.oidc_provider is None
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/callback", follow_redirects=False).status_code == 404


def test_oidc_secrets_codes_provider_tokens_and_hashes_never_enter_logs_or_audit(
    oidc_client, caplog
):
    client, main_module, fake = oidc_client
    caplog.set_level(logging.DEBUG)
    _, call = _begin_login(client, fake)
    flow = main_module.app_state.db.get_oidc_login_flow(hash_secret(call["state"]))
    authorization_code = "authorization-code-must-not-log"
    assert _finish_login(client, call["state"], authorization_code).status_code == 303
    raw_session = client.cookies.get(SESSION_COOKIE)
    session_id, _ = parse_session_token(raw_session)
    session_hash = main_module.app_state.db.get_actor_session(session_id).secret_hash

    # TestClient's httpx *client* logger records the request URL it constructs;
    # that is outside the server and necessarily sees the browser callback.
    # Inspect only application and supported-server logs here.  The dedicated
    # uvicorn access-log test below pins query redaction itself.
    combined_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "app"
        or record.name.startswith("app.")
        or record.name.startswith("uvicorn")
    )
    audit_text = str(read_audit(main_module.app_state.config.audit_path))
    combined = combined_logs + audit_text
    for secret in (
        authorization_code,
        call["state"],
        call["nonce"],
        flow.pkce_verifier,
        CLIENT_SECRET,
        raw_session,
        session_hash,
        "provider-access-token",
        "provider-refresh-token",
    ):
        assert secret not in combined


def test_uvicorn_access_filter_redacts_complete_callback_query():
    from app.main import _OIDC_ACCESS_LOG_FILTER

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1",
            "GET",
            "/auth/callback?code=authorization-code&state=opaque-state",
            "1.1",
            303,
        ),
        None,
    )
    assert _OIDC_ACCESS_LOG_FILTER.filter(record)
    rendered = record.getMessage()
    assert rendered.endswith('GET /auth/callback?[redacted] HTTP/1.1" 303')
    assert "authorization-code" not in rendered
    assert "opaque-state" not in rendered


def test_supported_launcher_disables_access_logging(monkeypatch):
    import app.main as main_module

    captured = {}
    fake_uvicorn = SimpleNamespace(run=lambda *args, **kwargs: captured.update(kwargs))
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(
        main_module,
        "load_app_config",
        lambda: AppConfig(servers=[], api_host="127.0.0.1", api_port=8765),
    )

    main_module.run()

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8765
    assert captured["access_log"] is False


def test_provider_exception_repr_is_credential_free():
    error: OIDCProviderError = OIDCValidationError()
    rendered = repr(error)
    assert "authorization" not in rendered.lower()
    assert CLIENT_SECRET not in rendered
