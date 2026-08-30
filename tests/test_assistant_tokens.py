"""DG-ASSISTANT-TOOLS v1 T-2 (packet P1a): assistant turn token identity helpers."""

from __future__ import annotations

import pytest

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
