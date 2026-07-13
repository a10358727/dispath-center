"""Focused tests for Goal 1 compatible credential-to-context resolution."""

from datetime import datetime, timezone

import pytest

from app.authentication import (
    LEGACY_ADMIN_ACTOR_ID,
    LEGACY_ADMIN_DISPLAY_NAME,
    LegacyActorCollisionError,
    ensure_legacy_admin_actor,
    extract_bearer_token,
    resolve_legacy_token_context,
    resolve_request_context,
    resolve_service_token_context,
    resolve_session_context,
)
from app.identity import (
    REDACTED,
    ActorType,
    generate_service_token,
    generate_session_token,
    parse_session_token,
    redact_session_token,
    verify_secret,
)


NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
FUTURE = "2026-07-13T12:00:00+00:00"
PAST = "2026-07-11T12:00:00+00:00"


def _database_text_values(db):
    tables = [
        row[0]
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    for table in tables:
        for row in db._conn.execute(f'SELECT * FROM "{table}"').fetchall():
            for value in row:
                if isinstance(value, bytes):
                    yield value.decode()
                elif isinstance(value, str):
                    yield value


def _make_human_session(db, *, expires_at=FUTURE):
    actor = db.insert_actor(
        actor_id="human-actor", actor_type="human", display_name="Ada"
    )
    db.insert_project("project-one", "/srv/project-one")
    membership = db.upsert_project_membership(
        project="project-one", actor_id=actor.id, role="operator"
    )
    issued = generate_session_token()
    session = db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at=expires_at,
    )
    return actor, membership, issued, session


def _make_service_token(db, *, expires_at=FUTURE, scopes=None):
    actor = db.insert_actor(
        actor_id="service-actor", actor_type="service", display_name="MCP reader"
    )
    db.insert_service_account(actor_id=actor.id, name="mcp-reader")
    db.insert_project("project-two", "/srv/project-two")
    membership = db.upsert_project_membership(
        project="project-two", actor_id=actor.id, role="viewer"
    )
    issued = generate_service_token()
    token = db.insert_service_account_token(
        token_id=issued.id,
        service_account_actor_id=actor.id,
        secret_hash=issued.secret_hash,
        scopes=scopes or ["project.view"],
        expires_at=expires_at,
    )
    return actor, membership, issued, token


def test_session_credential_format_hash_and_redaction_are_log_safe(db):
    actor = db.insert_actor(actor_id="human", actor_type="human", display_name="Human")
    issued = generate_session_token("12345678-1234-5678-1234-567812345678")
    session_id, secret = parse_session_token(issued.raw_token)
    db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at=FUTURE,
    )

    assert session_id == issued.id
    assert verify_secret(issued.raw_token, issued.secret_hash)
    assert redact_session_token(issued.raw_token) == f"dcsess_{issued.id}.{REDACTED}"
    assert issued.raw_token not in repr(issued)
    assert issued.secret_hash not in repr(issued)
    assert secret not in str(issued)
    persisted = list(_database_text_values(db))
    assert issued.raw_token not in persisted
    assert secret not in persisted


@pytest.mark.parametrize(
    "raw_session",
    [
        None,
        "",
        "not-a-session",
        "dcsess_not-a-uuid.secret",
        "dcsess_12345678-1234-5678-1234-567812345678.",
        "dcsess_12345678-1234-5678-1234-567812345678.bad.secret",
        "dcsess_12345678-1234-5678-1234-567812345678.bad+secret",
    ],
)
def test_malformed_or_missing_sessions_fail_closed(db, raw_session):
    assert resolve_session_context(db, raw_session, now=NOW) is None
    assert redact_session_token(raw_session) == REDACTED


def test_active_session_resolves_actor_and_memberships_without_secret(db):
    actor, membership, issued, _ = _make_human_session(db)

    context = resolve_session_context(db, issued.raw_token, now=NOW)

    assert context is not None
    assert context.actor_id == actor.id
    assert context.actor_type is ActorType.HUMAN
    assert context.authentication_method == "session"
    assert context.project_memberships == (membership,)
    assert context.service_scopes == frozenset()
    assert issued.raw_token not in repr(context)
    assert parse_session_token(issued.raw_token)[1] not in repr(context)


def test_session_rejects_wrong_secret_expiry_revocation_and_disabled_actor(db):
    actor, _, issued, session = _make_human_session(db)
    wrong = generate_session_token(issued.id)
    assert resolve_session_context(db, wrong.raw_token, now=NOW) is None

    db._conn.execute(
        "UPDATE actor_sessions SET expires_at = ? WHERE id = ?", (PAST, session.id)
    )
    db._conn.commit()
    assert resolve_session_context(db, issued.raw_token, now=NOW) is None

    db._conn.execute(
        "UPDATE actor_sessions SET expires_at = ? WHERE id = ?", (FUTURE, session.id)
    )
    db._conn.commit()
    assert db.revoke_actor_session(session.id, revoked_at=NOW.isoformat())
    assert resolve_session_context(db, issued.raw_token, now=NOW) is None

    second = generate_session_token()
    db.insert_actor_session(
        session_id=second.id,
        actor_id=actor.id,
        secret_hash=second.secret_hash,
        expires_at=FUTURE,
    )
    db.update_actor(actor.id, disabled_at=NOW.isoformat())
    assert resolve_session_context(db, second.raw_token, now=NOW) is None


def test_service_actor_cannot_authenticate_through_human_session_transport(db):
    actor = db.insert_actor(
        actor_id="session-shaped-service",
        actor_type="service",
        display_name="Service",
    )
    db.insert_service_account(actor_id=actor.id, name="service")
    issued = generate_session_token()
    db.insert_actor_session(
        session_id=issued.id,
        actor_id=actor.id,
        secret_hash=issued.secret_hash,
        expires_at=FUTURE,
    )

    assert resolve_session_context(db, issued.raw_token, now=NOW) is None


@pytest.mark.parametrize("stored_expiry", ["", "not-a-time", "2026-07-13T12:00:00"])
def test_malformed_or_naive_stored_session_expiry_fails_closed(db, stored_expiry):
    _, _, issued, session = _make_human_session(db)
    db._conn.execute(
        "UPDATE actor_sessions SET expires_at = ? WHERE id = ?",
        (stored_expiry, session.id),
    )
    db._conn.commit()
    assert resolve_session_context(db, issued.raw_token, now=NOW) is None


def test_resolution_requires_timezone_aware_now(db):
    _, _, issued, _ = _make_human_session(db)
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_session_context(db, issued.raw_token, now=datetime(2026, 7, 12))
    with pytest.raises(TypeError, match="timezone-aware"):
        resolve_session_context(db, issued.raw_token, now="2026-07-12")


def test_service_token_feature_switch_scopes_memberships_and_no_touch(db):
    actor, membership, issued, token = _make_service_token(
        db, scopes=["project.view", "project.operate"]
    )

    assert (
        resolve_service_token_context(db, issued.raw_token, enabled=False, now=NOW)
        is None
    )
    context = resolve_service_token_context(db, issued.raw_token, enabled=True, now=NOW)

    assert context is not None
    assert context.actor_id == actor.id
    assert context.actor_type is ActorType.SERVICE
    assert context.authentication_method == "service_token"
    assert context.service_token_id == token.id
    assert context.service_scopes == frozenset({"project.view", "project.operate"})
    assert context.project_memberships == (membership,)
    assert db.get_service_account_token(token.id).last_used_at is None
    assert issued.raw_token not in repr(context)


def test_service_token_rejects_wrong_secret_expiry_revocation_and_disabled_actor(db):
    actor, _, issued, token = _make_service_token(db)
    wrong = generate_service_token(issued.id)
    assert resolve_service_token_context(db, wrong.raw_token, enabled=True, now=NOW) is None

    db._conn.execute(
        "UPDATE service_account_tokens SET expires_at = ? WHERE id = ?", (PAST, token.id)
    )
    db._conn.commit()
    assert resolve_service_token_context(db, issued.raw_token, enabled=True, now=NOW) is None

    db._conn.execute(
        "UPDATE service_account_tokens SET expires_at = ? WHERE id = ?", (FUTURE, token.id)
    )
    db._conn.commit()
    assert db.revoke_service_account_token(token.id, revoked_at=NOW.isoformat())
    assert resolve_service_token_context(db, issued.raw_token, enabled=True, now=NOW) is None

    second = generate_service_token()
    db.insert_service_account_token(
        token_id=second.id,
        service_account_actor_id=actor.id,
        secret_hash=second.secret_hash,
        scopes=["project.view"],
        expires_at=FUTURE,
    )
    db.update_actor(actor.id, disabled_at=NOW.isoformat())
    assert resolve_service_token_context(db, second.raw_token, enabled=True, now=NOW) is None


def test_service_token_malformed_storage_fails_closed(db):
    _, _, issued, token = _make_service_token(db)
    db._conn.execute(
        "UPDATE service_account_tokens SET expires_at = ? WHERE id = ?",
        ("invalid", token.id),
    )
    db._conn.commit()
    assert resolve_service_token_context(db, issued.raw_token, enabled=True, now=NOW) is None

    db._conn.execute(
        "UPDATE service_account_tokens SET expires_at = ?, scopes = ? WHERE id = ?",
        (FUTURE, "{}", token.id),
    )
    db._conn.commit()
    assert resolve_service_token_context(db, issued.raw_token, enabled=True, now=NOW) is None


def test_legacy_actor_bootstrap_is_idempotent_and_resolver_is_read_only(db):
    assert resolve_legacy_token_context(
        db, "shared", configured_token="shared", enabled=True
    ) is None

    first = ensure_legacy_admin_actor(db)
    second = ensure_legacy_admin_actor(db)
    context = resolve_legacy_token_context(
        db, "shared", configured_token="shared", enabled=True
    )

    assert first == second
    assert first.id == LEGACY_ADMIN_ACTOR_ID
    assert first.actor_type is ActorType.LEGACY
    assert first.display_name == LEGACY_ADMIN_DISPLAY_NAME
    assert first.platform_admin is True
    assert context is not None
    assert context.actor_id == LEGACY_ADMIN_ACTOR_ID
    assert context.authentication_method == "legacy_shared_token"
    assert len(db.list_actors(actor_type="legacy")) == 1


def test_legacy_actor_is_durable_across_database_reopen(tmp_path):
    from app.db import Database

    path = tmp_path / "identity.db"
    first_db = Database(str(path))
    ensure_legacy_admin_actor(first_db)
    first_db.close()

    second_db = Database(str(path))
    try:
        actor = ensure_legacy_admin_actor(second_db)
        assert actor.id == LEGACY_ADMIN_ACTOR_ID
        assert len(second_db.list_actors(actor_type="legacy")) == 1
    finally:
        second_db.close()


def test_legacy_actor_collision_is_never_overwritten(db):
    db.insert_actor(
        actor_id=LEGACY_ADMIN_ACTOR_ID,
        actor_type="human",
        display_name="Unrelated human",
        platform_admin=False,
    )

    with pytest.raises(LegacyActorCollisionError, match="conflicting"):
        ensure_legacy_admin_actor(db)

    actor = db.get_actor(LEGACY_ADMIN_ACTOR_ID)
    assert actor.actor_type is ActorType.HUMAN
    assert actor.display_name == "Unrelated human"
    assert actor.platform_admin is False
    assert resolve_legacy_token_context(
        db, "shared", configured_token="shared", enabled=True
    ) is None


def test_legacy_token_switch_mismatch_missing_and_disabled_fail_closed(db):
    actor = ensure_legacy_admin_actor(db)
    for presented, configured, enabled in [
        ("wrong", "shared", True),
        (None, "shared", True),
        ("shared", None, True),
        ("shared", "shared", False),
        ("共享", "不同", True),
    ]:
        assert resolve_legacy_token_context(
            db, presented, configured_token=configured, enabled=enabled
        ) is None

    db.update_actor(actor.id, disabled_at=NOW.isoformat())
    assert resolve_legacy_token_context(
        db, "shared", configured_token="shared", enabled=True
    ) is None


def test_combined_resolver_precedence_session_then_service_then_legacy(db):
    human, _, session, _ = _make_human_session(db)
    service, _, service_token, _ = _make_service_token(db)
    legacy = ensure_legacy_admin_actor(db)
    common = {
        "authorization": f"Bearer {service_token.raw_token}",
        "legacy_token": "shared",
        "configured_legacy_token": "shared",
        "service_token_auth_enabled": True,
        "legacy_shared_token_enabled": True,
        "now": NOW,
    }

    assert resolve_request_context(db, session_token=session.raw_token, **common).actor_id == human.id
    assert resolve_request_context(db, session_token="invalid", **common).actor_id == service.id
    assert resolve_request_context(
        db,
        session_token="invalid",
        **{**common, "service_token_auth_enabled": False},
    ).actor_id == legacy.id


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic abc", "Bearer", "Bearer one two", "NotBearer token"],
)
def test_bearer_header_and_combined_malformed_credentials_fail_closed(db, authorization):
    assert extract_bearer_token(authorization) is None
    assert resolve_request_context(
        db,
        session_token="malformed",
        authorization=authorization,
        legacy_token="wrong",
        configured_legacy_token="shared",
        service_token_auth_enabled=True,
        now=NOW,
    ) is None


def test_bearer_extraction_is_case_insensitive_and_returns_only_credential():
    assert extract_bearer_token("  bEaReR opaque-token  ") == "opaque-token"
