"""DG-ASSISTANT-TOOLS v1 T-2 (packet P1a): assistant turn token identity helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.assistant_tokens import (
    issue_assistant_turn_token,
    purge_stale_assistant_turn_tokens,
    revoke_assistant_turn_token,
)
from app.audit import read_audit
from app.authentication import (
    LEGACY_ADMIN_ACTOR_ID,
    ensure_legacy_admin_actor,
    resolve_request_context,
)
from app.db import Database
from app.identity import (
    ASSISTANT_TURN_TOKEN_PREFIX,
    REDACTED,
    generate_assistant_turn_token,
    generate_service_token,
    hash_secret,
    is_assistant_turn_token,
    parse_assistant_turn_token,
    parse_service_token,
    redact_token,
    verify_secret,
)


def test_generate_assistant_turn_token_has_dat_prefix_and_hashes_the_full_bearer():
    issued = generate_assistant_turn_token()
    assert issued.raw_token.startswith(ASSISTANT_TURN_TOKEN_PREFIX)
    token_id, secret = parse_assistant_turn_token(issued.raw_token)
    assert token_id == issued.id
    assert secret and "." not in secret
    assert issued.secret_hash == hash_secret(issued.raw_token)
    assert verify_secret(issued.raw_token, issued.secret_hash)
    assert not verify_secret(issued.raw_token + "x", issued.secret_hash)
    assert "raw_token" not in repr(issued) and issued.raw_token not in repr(issued)
    assert str(issued) == f"{ASSISTANT_TURN_TOKEN_PREFIX}{issued.id}.{REDACTED}"


def test_generate_assistant_turn_token_accepts_explicit_uuid_and_rejects_garbage():
    fixed = "11111111-2222-4333-8444-555555555555"
    assert generate_assistant_turn_token(fixed).id == fixed
    with pytest.raises(ValueError):
        generate_assistant_turn_token("not-a-uuid")


def test_parse_assistant_turn_token_rejects_other_token_kinds_and_malformed_values():
    service = generate_service_token().raw_token
    with pytest.raises(ValueError):
        parse_assistant_turn_token(service)
    with pytest.raises(ValueError):
        parse_service_token(generate_assistant_turn_token().raw_token)
    for bad in ("", "dat_", "dat_abc.def", "dat_11111111-2222-4333-8444-555555555555", None, 42):
        with pytest.raises(ValueError):
            parse_assistant_turn_token(bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        parse_assistant_turn_token("dat_11111111-2222-4333-8444-555555555555.bad.secret")


def test_is_assistant_turn_token_is_a_pure_shape_check():
    assert is_assistant_turn_token(generate_assistant_turn_token().raw_token)
    assert not is_assistant_turn_token(generate_service_token().raw_token)
    assert not is_assistant_turn_token(None)
    assert not is_assistant_turn_token("")


def test_redact_token_never_leaks_assistant_turn_secret():
    issued = generate_assistant_turn_token()
    redacted = redact_token(issued.raw_token)
    assert redacted == f"{ASSISTANT_TURN_TOKEN_PREFIX}{issued.id}.{REDACTED}"
    assert issued.raw_token.split(".", 1)[1] not in redacted
    assert redact_token("dat_garbage") == REDACTED


# ---------------------------------------------------------------------------
# Resolution seam + lifecycle (packet P1a sub-packets b/c)
# ---------------------------------------------------------------------------


@pytest.fixture
def token_db(tmp_path):
    database = Database(str(tmp_path / "tokens.db"))
    ensure_legacy_admin_actor(database)
    yield database, str(tmp_path / "audit.jsonl")
    database.close()


def _resolve(db, raw, *, enabled=True, now=None, configured="shared-secret"):
    return resolve_request_context(
        db,
        legacy_token=raw,
        configured_legacy_token=configured,
        legacy_shared_token_enabled=True,
        assistant_turn_tokens_enabled=enabled,
        now=now,
    )


def test_issued_turn_token_resolves_to_the_bound_human_actor(token_db):
    db, audit_path = token_db
    issued = issue_assistant_turn_token(
        db, actor_id=LEGACY_ADMIN_ACTOR_ID, turn_ref="sess:1", ttl_sec=150, audit_path=audit_path
    )
    context = _resolve(db, issued.raw_token)
    assert context is not None
    assert context.actor is not None and context.actor.id == LEGACY_ADMIN_ACTOR_ID
    assert context.authentication_method == "assistant_turn_token"
    assert context.assistant_turn_token_id == issued.token_id
    assert db.get_assistant_turn_token(issued.token_id)["last_used_at"] is not None
    actions = [row["action"] for row in read_audit(audit_path)]
    assert "assistant_turn_token_issue" in actions
    assert issued.raw_token not in open(audit_path, encoding="utf-8").read()
    assert issued.raw_token.split(".", 1)[1] not in open(audit_path, encoding="utf-8").read()


def test_turn_token_fails_closed_when_flag_off_wrong_secret_expired_revoked_or_disabled(token_db):
    db, audit_path = token_db
    issued = issue_assistant_turn_token(
        db, actor_id=LEGACY_ADMIN_ACTOR_ID, turn_ref="sess:1", ttl_sec=150, audit_path=audit_path
    )
    assert _resolve(db, issued.raw_token, enabled=False) is None
    assert _resolve(db, issued.raw_token[:-1] + ("A" if issued.raw_token[-1] != "A" else "B")) is None
    assert _resolve(db, "dat_11111111-2222-4333-8444-555555555555.unknown") is None
    assert _resolve(db, issued.raw_token, now=datetime.now(timezone.utc) + timedelta(seconds=151)) is None
    assert revoke_assistant_turn_token(db, token_id=issued.token_id, audit_path=audit_path) is True
    assert _resolve(db, issued.raw_token) is None
    assert revoke_assistant_turn_token(db, token_id=issued.token_id, audit_path=audit_path) is False
    assert "assistant_turn_token_revoke" in [row["action"] for row in read_audit(audit_path)]

    fresh = issue_assistant_turn_token(
        db, actor_id=LEGACY_ADMIN_ACTOR_ID, turn_ref="sess:2", ttl_sec=150, audit_path=audit_path
    )
    with db.cursor() as cur:
        cur.execute("UPDATE actors SET disabled_at = ? WHERE id = ?", ("2026-08-30T00:00:00+00:00", LEGACY_ADMIN_ACTOR_ID))
    assert _resolve(db, fresh.raw_token) is None


def test_dat_value_never_matches_the_shared_legacy_token(token_db):
    db, audit_path = token_db
    issued = issue_assistant_turn_token(
        db, actor_id=LEGACY_ADMIN_ACTOR_ID, turn_ref="sess:1", ttl_sec=150, audit_path=audit_path
    )
    # Even when the operator configured the very same string as AUTH_TOKEN, a
    # `dat_` value is only ever evaluated as a turn token.
    assert _resolve(db, issued.raw_token, enabled=False, configured=issued.raw_token) is None
    # And the shared token itself still works through the legacy branch.
    shared = _resolve(db, "shared-secret", enabled=True)
    assert shared is not None and shared.authentication_method == "legacy_shared_token"


def test_purge_keeps_live_tokens_and_drops_old_ones(token_db):
    db, audit_path = token_db
    old = issue_assistant_turn_token(
        db,
        actor_id=LEGACY_ADMIN_ACTOR_ID,
        turn_ref="sess:old",
        ttl_sec=30,
        audit_path=audit_path,
        now=datetime.now(timezone.utc) - timedelta(days=2),
    )
    live = issue_assistant_turn_token(
        db, actor_id=LEGACY_ADMIN_ACTOR_ID, turn_ref="sess:live", ttl_sec=150, audit_path=audit_path
    )
    assert purge_stale_assistant_turn_tokens(db, keep_hours=24) == 1
    assert db.get_assistant_turn_token(old.token_id) is None
    assert db.get_assistant_turn_token(live.token_id) is not None
