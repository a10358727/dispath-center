"""Focused tests for Goal 1's dependency-free identity foundation."""

from dataclasses import FrozenInstanceError
import hashlib
import re
import uuid

import pytest

from app.identity import (
    REDACTED,
    Actor,
    ActorSession,
    ActorType,
    OIDCIdentity,
    OIDCLoginFlow,
    ProjectMembership,
    ProjectRole,
    RequestContext,
    ServiceAccount,
    ServiceAccountToken,
    generate_secret,
    generate_service_token,
    hash_secret,
    parse_service_token,
    redact_token,
    verify_secret,
)


def _database_text_values(database):
    """Yield every persisted TEXT/BLOB value without touching runtime files."""

    tables = [
        row[0]
        for row in database._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        for row in database._conn.execute(f'SELECT * FROM "{table}"').fetchall():
            for value in row:
                if isinstance(value, (str, bytes)):
                    yield value.decode() if isinstance(value, bytes) else value


def test_actor_and_project_role_values_normalize_and_validate():
    actor = Actor(id="actor-1", actor_type="human", display_name="Ada")
    membership = ProjectMembership(
        project_id="project-1",
        actor_id=actor.id,
        role="operator",
    )

    assert actor.actor_type is ActorType.HUMAN
    assert str(actor.actor_type) == "human"
    assert membership.role is ProjectRole.OPERATOR
    assert str(membership.role) == "operator"

    with pytest.raises(ValueError):
        Actor(id="bad", actor_type="robot", display_name="Invalid")
    with pytest.raises(ValueError):
        ProjectMembership(project_id="project-1", actor_id="actor-1", role="owner")


def test_schema_shaped_identity_dataclasses_preserve_non_secret_values():
    identity = OIDCIdentity(
        id="identity-1",
        actor_id="actor-1",
        issuer="https://issuer.example",
        subject="subject-1",
        email="ada@example.com",
        created_at="2026-07-12T00:00:00+00:00",
    )
    account = ServiceAccount(
        actor_id="service-1",
        name="mcp-reader",
        created_at="2026-07-12T00:00:00+00:00",
        description="Read-only MCP account",
        created_by_actor_id="actor-1",
    )

    assert identity.subject == "subject-1"
    assert identity.email == "ada@example.com"
    assert account.name == "mcp-reader"
    assert account.created_by_actor_id == "actor-1"


def test_generate_secret_is_urlsafe_high_entropy_and_unique():
    first = generate_secret()
    second = generate_secret()

    assert first != second
    assert len(first) >= 43  # 32 bytes encoded without base64 padding.
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first)


@pytest.mark.parametrize("num_bytes", [0, -1])
def test_generate_secret_rejects_non_positive_entropy(num_bytes):
    with pytest.raises(ValueError):
        generate_secret(num_bytes)


@pytest.mark.parametrize("num_bytes", [True, 1.5, "32"])
def test_generate_secret_rejects_non_integer_entropy(num_bytes):
    with pytest.raises(TypeError):
        generate_secret(num_bytes)


def test_hash_and_verify_secret_use_sha256_and_reject_mismatch():
    raw = "one-time-secret"
    digest = hash_secret(raw)

    assert digest == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert len(digest) == 64
    assert verify_secret(raw, digest) is True
    assert verify_secret("different", digest) is False
    assert verify_secret(raw, "not-a-digest") is False
    assert verify_secret(raw, None) is False


def test_hash_secret_rejects_non_string_input():
    with pytest.raises(TypeError):
        hash_secret(b"secret")


def test_service_token_format_parse_hash_and_redaction():
    issued = generate_service_token()
    parsed_id, secret = parse_service_token(issued.raw_token)

    assert parsed_id == issued.id
    assert str(uuid.UUID(issued.id)) == issued.id
    assert re.fullmatch(r"[A-Za-z0-9_-]+", secret)
    assert issued.raw_token == f"dcs_{issued.id}.{secret}"
    assert verify_secret(issued.raw_token, issued.secret_hash) is True

    redacted = redact_token(issued.raw_token)
    assert redacted == f"dcs_{issued.id}.{REDACTED}"
    assert secret not in redacted
    assert issued.raw_token not in repr(issued)
    assert issued.secret_hash not in repr(issued)
    assert str(issued) == redacted


def test_service_token_accepts_explicit_uuid_and_rejects_invalid_id():
    token_id = "12345678-1234-5678-1234-567812345678"
    issued = generate_service_token(token_id)

    assert issued.id == token_id
    assert issued.raw_token.startswith(f"dcs_{token_id}.")

    with pytest.raises(ValueError):
        generate_service_token("not-a-uuid")


@pytest.mark.parametrize(
    "raw_token",
    [
        "",
        "not-a-token",
        "dcs_not-a-uuid.secret",
        "dcs_12345678-1234-5678-1234-567812345678.",
        "dcs_12345678-1234-5678-1234-567812345678.secret.with-dot",
        "dcs_12345678-1234-5678-1234-567812345678.not+urlsafe",
    ],
)
def test_parse_service_token_rejects_invalid_formats(raw_token):
    with pytest.raises(ValueError, match="invalid service token"):
        parse_service_token(raw_token)


def test_redact_token_never_echoes_invalid_or_non_service_credentials():
    for credential in (None, "", "session-secret", "Bearer something", "dcs_bad.secret"):
        assert redact_token(credential) == REDACTED


def test_credential_bearing_dataclass_reprs_exclude_secrets_and_hashes():
    flow = OIDCLoginFlow(
        state_hash="state-hash-must-not-log",
        nonce_hash="nonce-hash-must-not-log",
        pkce_verifier="pkce-verifier-must-not-log",
        created_at="2026-07-12T00:00:00+00:00",
        expires_at="2026-07-12T00:10:00+00:00",
        return_to="/projects",
    )
    session = ActorSession(
        id="session-1",
        actor_id="actor-1",
        secret_hash="session-hash-must-not-log",
        created_at="2026-07-12T00:00:00+00:00",
        expires_at="2026-07-12T08:00:00+00:00",
    )
    token = ServiceAccountToken(
        id="token-1",
        service_account_actor_id="service-1",
        secret_hash="token-hash-must-not-log",
        created_at="2026-07-12T00:00:00+00:00",
        expires_at="2026-08-12T00:00:00+00:00",
        scopes=["project.view"],
    )

    flow_repr = repr(flow)
    session_repr = repr(session)
    token_repr = repr(token)
    assert "state-hash-must-not-log" not in flow_repr
    assert "nonce-hash-must-not-log" not in flow_repr
    assert "pkce-verifier-must-not-log" not in flow_repr
    assert "session-hash-must-not-log" not in session_repr
    assert "token-hash-must-not-log" not in token_repr
    assert "return_to='/projects'" in flow_repr
    assert "id='session-1'" in session_repr
    assert "scopes=['project.view']" in token_repr


def test_request_context_is_frozen_and_normalizes_collections():
    actor = Actor(
        id="actor-1",
        actor_type=ActorType.HUMAN,
        display_name="Ada",
        platform_admin=True,
    )
    membership = ProjectMembership(
        project_id="project-1",
        actor_id=actor.id,
        role=ProjectRole.ADMIN,
    )
    context = RequestContext(
        actor=actor,
        authentication_method="session",
        service_scopes={"project.view", "project.operate"},
        project_memberships=[membership],
    )

    assert context.actor_id == "actor-1"
    assert context.actor_type is ActorType.HUMAN
    assert context.platform_admin is True
    assert context.service_scopes == frozenset({"project.view", "project.operate"})
    assert context.project_memberships == (membership,)

    with pytest.raises(FrozenInstanceError):
        context.authentication_method = "legacy_token"


def test_anonymous_request_context_has_no_actor_identity():
    context = RequestContext()

    assert context.actor is None
    assert context.actor_id is None
    assert context.actor_type is None
    assert context.platform_admin is False
    assert context.authentication_method == "anonymous"


def test_request_context_rejects_empty_authentication_method():
    with pytest.raises(ValueError, match="authentication_method"):
        RequestContext(authentication_method="")


def test_database_identity_crud_and_canonical_project_membership(db):
    human = db.insert_actor(
        actor_id="human-1",
        actor_type="human",
        display_name="Ada",
        email="ada@example.com",
    )
    service_actor = db.insert_actor(
        actor_id="service-1",
        actor_type=ActorType.SERVICE,
        display_name="MCP Reader",
    )
    project_uuid = db.insert_project("project-one", "/srv/project-one")

    oidc = db.insert_oidc_identity(
        identity_id="identity-1",
        actor_id=human.id,
        issuer="https://issuer.example",
        subject="subject-1",
        email=human.email,
    )
    account = db.insert_service_account(
        actor_id=service_actor.id,
        name="mcp-reader",
        created_by_actor_id=human.id,
    )
    membership = db.upsert_project_membership(
        project="project-one",
        actor_id=human.id,
        role="operator",
        created_by_actor_id=human.id,
    )

    assert db.get_actor(human.id) == human
    assert db.get_oidc_identity_by_subject(oidc.issuer, oidc.subject) == oidc
    assert db.get_service_account(account.actor_id) == account
    assert membership.project_id == project_uuid
    assert membership.role is ProjectRole.OPERATOR
    assert db.get_project_membership(project_uuid, human.id) == membership

    updated = db.upsert_project_membership(
        project=project_uuid,
        actor_id=human.id,
        role=ProjectRole.ADMIN,
    )
    assert updated.project_id == project_uuid
    assert updated.role is ProjectRole.ADMIN
    assert db.delete_project_membership(project_uuid, human.id) is True
    assert db.delete_project_membership(project_uuid, human.id) is False


def test_oidc_login_flow_consumption_is_expiring_single_use_and_clears_pkce(db):
    flow = db.insert_oidc_login_flow(
        state_hash="state-hash",
        nonce_hash="nonce-hash",
        pkce_verifier="temporary-verifier",
        expires_at="2026-07-12T00:10:00+00:00",
        return_to="/projects",
    )
    assert flow.pkce_verifier == "temporary-verifier"

    consumed = db.consume_oidc_login_flow(
        "state-hash", now="2026-07-12T00:05:00+00:00"
    )
    assert consumed is not None
    assert consumed.pkce_verifier == "temporary-verifier"
    assert consumed.consumed_at == "2026-07-12T00:05:00+00:00"
    assert db.get_oidc_login_flow("state-hash").pkce_verifier is None
    assert db.consume_oidc_login_flow(
        "state-hash", now="2026-07-12T00:06:00+00:00"
    ) is None

    db.insert_oidc_login_flow(
        state_hash="expired-state",
        nonce_hash="nonce-hash-2",
        pkce_verifier="expired-verifier",
        expires_at="2026-07-12T00:01:00+00:00",
    )
    assert db.consume_oidc_login_flow(
        "expired-state", now="2026-07-12T00:02:00+00:00"
    ) is None


def test_session_and_service_token_persist_hashes_only_and_revoke_idempotently(db):
    creator = db.insert_actor(
        actor_id="creator-1", actor_type="human", display_name="Creator"
    )
    service_actor = db.insert_actor(
        actor_id="service-2", actor_type="service", display_name="Automation"
    )
    db.insert_service_account(
        actor_id=service_actor.id,
        name="automation",
        created_by_actor_id=creator.id,
    )

    raw_session = generate_secret()
    session = db.insert_actor_session(
        session_id="session-1",
        actor_id=creator.id,
        secret_hash=hash_secret(raw_session),
        expires_at="2026-07-13T00:00:00+00:00",
    )
    issued = generate_service_token()
    token = db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=service_actor.id,
        secret_hash=issued.secret_hash,
        scopes=["project.view"],
        expires_at="2026-08-12T00:00:00+00:00",
        created_by_actor_id=creator.id,
    )

    assert session.secret_hash == hash_secret(raw_session)
    assert token.secret_hash == issued.secret_hash
    assert token.scopes == ["project.view"]
    all_values = list(_database_text_values(db))
    assert raw_session not in all_values
    assert issued.raw_token not in all_values
    assert parse_service_token(issued.raw_token)[1] not in all_values

    assert db.revoke_actor_session(session.id, revoked_at="2026-07-12T01:00:00+00:00") is True
    assert db.revoke_actor_session(session.id) is False
    assert db.revoke_service_account_token(
        token.id, revoked_at="2026-07-12T01:00:00+00:00"
    ) is True
    assert db.revoke_service_account_token(token.id) is False
    assert db.get_actor_session(session.id).revoked_at is not None
    assert db.get_service_account_token(token.id).revoked_at is not None


def test_database_rejects_invalid_identity_references_and_roles(db):
    with pytest.raises(ValueError, match="actor missing"):
        db.insert_oidc_identity(
            actor_id="missing", issuer="https://issuer.example", subject="sub"
        )

    human = db.insert_actor(
        actor_id="human-not-service", actor_type="human", display_name="Human"
    )
    with pytest.raises(ValueError, match="service actor"):
        db.insert_service_account(actor_id=human.id, name="invalid")

    db.insert_project("project-two", "/srv/project-two")
    with pytest.raises(ValueError):
        db.upsert_project_membership(
            project="project-two", actor_id=human.id, role="owner"
        )
