"""No-network tests for the injected OIDC provider and Authlib adapter."""

from __future__ import annotations

import base64
import logging
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.oidc import (
    AuthlibOIDCProvider,
    OIDCClaims,
    OIDCDiscoveryError,
    OIDCFlowSecrets,
    OIDCProvider,
    OIDCTokenExchangeError,
    OIDCValidationError,
    build_oidc_provider,
    extract_oidc_nonce,
    generate_oidc_flow_secrets,
    pkce_s256_challenge,
)


ISSUER = "https://issuer.example.test"
METADATA_URL = f"{ISSUER}/.well-known/openid-configuration"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/authorize"
TOKEN_ENDPOINT = f"{ISSUER}/token"
JWKS_URI = f"{ISSUER}/jwks"
CLIENT_ID = "dispatch-center"
CLIENT_SECRET = "CLIENT-SECRET-MUST-NOT-LOG"
REDIRECT_URI = "https://dispatch.example.test/auth/callback"
CODE = "AUTHORIZATION-CODE-MUST-NOT-LOG"
ACCESS_TOKEN = "PROVIDER-ACCESS-TOKEN-MUST-NOT-PERSIST"
REFRESH_TOKEN = "PROVIDER-REFRESH-TOKEN-MUST-NOT-PERSIST"


class FakeOIDCServer:
    """An in-process discovery/JWKS/token endpoint behind MockTransport."""

    def __init__(
        self,
        *,
        claim_overrides=None,
        signing_key=None,
        metadata_overrides=None,
        token_overrides=None,
        token_status_code=200,
    ):
        self.public_key = RSAKey.generate_key(
            2048,
            parameters={"kid": "provider-key", "alg": "RS256", "use": "sig"},
            private=True,
        )
        self.signing_key = signing_key or self.public_key
        self.claim_overrides = claim_overrides or {}
        self.metadata_overrides = metadata_overrides or {}
        self.token_overrides = token_overrides or {}
        self.token_status_code = token_status_code
        self.expected_nonce = "nonce-not-configured"
        self.expected_verifier = "verifier-not-configured"
        self.discovery_requests = 0
        self.jwks_requests = 0
        self.token_requests = 0
        self.last_token_form = None
        self.last_authorization_header = None
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == METADATA_URL:
            self.discovery_requests += 1
            metadata = {
                "issuer": ISSUER,
                "authorization_endpoint": AUTHORIZATION_ENDPOINT,
                "token_endpoint": TOKEN_ENDPOINT,
                "jwks_uri": JWKS_URI,
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "code_challenge_methods_supported": ["S256"],
                "id_token_signing_alg_values_supported": ["RS256", "none"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic"],
            }
            metadata.update(self.metadata_overrides)
            return httpx.Response(200, json=metadata)

        if str(request.url) == JWKS_URI:
            self.jwks_requests += 1
            return httpx.Response(
                200,
                json={"keys": [self.public_key.as_dict(private=False)]},
            )

        if str(request.url) == TOKEN_ENDPOINT:
            self.token_requests += 1
            self.last_authorization_header = request.headers.get("Authorization")
            form = parse_qs(request.content.decode("ascii"), keep_blank_values=True)
            self.last_token_form = form
            if (
                form.get("grant_type") != ["authorization_code"]
                or form.get("code") != [CODE]
                or form.get("code_verifier") != [self.expected_verifier]
                or form.get("redirect_uri") != [REDIRECT_URI]
            ):
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant", "error_description": CODE},
                )

            now = int(time.time())
            claims = {
                "iss": ISSUER,
                "sub": "subject-123",
                "aud": CLIENT_ID,
                "exp": now + 300,
                "iat": now,
                "nonce": self.expected_nonce,
                "name": "Ada Operator",
                "email": "ada@example.test",
            }
            claims.update(self.claim_overrides)
            id_token = jwt.encode(
                {"alg": "RS256", "kid": "provider-key", "typ": "JWT"},
                claims,
                self.signing_key,
                algorithms=["RS256"],
            )
            token = {
                "token_type": "Bearer",
                "access_token": ACCESS_TOKEN,
                "refresh_token": REFRESH_TOKEN,
                "id_token": id_token,
            }
            token.update(self.token_overrides)
            return httpx.Response(self.token_status_code, json=token)

        raise AssertionError(f"unexpected fake-provider request: {request.method}")


def make_provider(server: FakeOIDCServer, **overrides) -> AuthlibOIDCProvider:
    values = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "metadata_url": METADATA_URL,
        "transport": server.transport,
        "timeout": 1.0,
    }
    values.update(overrides)
    provider = AuthlibOIDCProvider(**values)
    server.expected_nonce = "nonce-value"
    server.expected_verifier = generate_oidc_flow_secrets().code_verifier
    return provider


def all_provider_values(provider: AuthlibOIDCProvider):
    for value in provider.__dict__.values():
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            yield from (item for item in value.values() if isinstance(item, str))


def test_flow_secret_generation_is_restart_recoverable_and_repr_safe():
    first = generate_oidc_flow_secrets()
    second = generate_oidc_flow_secrets()

    assert isinstance(first, OIDCFlowSecrets)
    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier
    assert extract_oidc_nonce(first.state) == first.nonce
    assert first.state.count(".") == 1
    assert 43 <= len(first.code_verifier) <= 128
    assert first.code_challenge == pkce_s256_challenge(first.code_verifier)
    assert repr(first) == "OIDCFlowSecrets()"
    for raw in (first.state, first.nonce, first.code_verifier, first.code_challenge):
        assert raw not in repr(first)


def test_pkce_s256_uses_the_rfc_7636_vector_and_rejects_bad_verifier():
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_s256_challenge(verifier) == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    with pytest.raises(ValueError, match="invalid PKCE verifier"):
        pkce_s256_challenge("too-short")
    # RFC 7636's unreserved verifier alphabet includes both dot and tilde.
    assert len(pkce_s256_challenge("a" * 41 + ".~")) == 43
    with pytest.raises(ValueError, match="invalid PKCE verifier"):
        pkce_s256_challenge("a" * 42 + "=")
    with pytest.raises(ValueError, match="invalid OIDC state"):
        extract_oidc_nonce("not-composite")


@pytest.mark.asyncio
async def test_authorization_url_uses_explicit_s256_challenge_without_verifier():
    server = FakeOIDCServer()
    provider = make_provider(server, scopes=("openid", "email", "groups"), leeway=7)
    flow = generate_oidc_flow_secrets()

    url = await provider.authorization_url(
        state=flow.state,
        nonce=flow.nonce,
        code_challenge=flow.code_challenge,
        redirect_uri=REDIRECT_URI,
    )

    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZATION_ENDPOINT
    assert query == {
        "response_type": ["code"],
        "client_id": [CLIENT_ID],
        "redirect_uri": [REDIRECT_URI],
        "scope": ["openid email groups"],
        "state": [flow.state],
        "nonce": [flow.nonce],
        "code_challenge": [flow.code_challenge],
        "code_challenge_method": ["S256"],
    }
    assert flow.code_verifier not in url
    assert CLIENT_SECRET not in url
    assert server.discovery_requests == 1
    assert server.token_requests == 0


@pytest.mark.asyncio
async def test_discovered_authorization_endpoint_preserves_safe_query_parameters():
    server = FakeOIDCServer(
        metadata_overrides={
            "authorization_endpoint": AUTHORIZATION_ENDPOINT + "?tenant=dispatch"
        }
    )
    provider = make_provider(server)
    flow = generate_oidc_flow_secrets()

    url = await provider.authorization_url(
        state=flow.state,
        nonce=flow.nonce,
        code_challenge=flow.code_challenge,
        redirect_uri=REDIRECT_URI,
    )

    query = parse_qs(urlsplit(url).query)
    assert query["tenant"] == ["dispatch"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://issuer.example.test/authorize",
        "https://user:password@issuer.example.test/authorize",
        "https://issuer.example.test/authorize#fragment",
        " https://issuer.example.test/authorize",
        "https://issuer.example.test/authorize\n",
    ],
    ids=["http", "userinfo", "fragment", "leading-space", "control"],
)
@pytest.mark.asyncio
async def test_discovery_rejects_unsafe_authorization_endpoint(endpoint):
    server = FakeOIDCServer(
        metadata_overrides={"authorization_endpoint": endpoint}
    )
    provider = make_provider(server)
    flow = generate_oidc_flow_secrets()

    with pytest.raises(OIDCDiscoveryError, match="provider discovery failed"):
        await provider.authorization_url(
            state=flow.state,
            nonce=flow.nonce,
            code_challenge=flow.code_challenge,
            redirect_uri=REDIRECT_URI,
        )


@pytest.mark.asyncio
async def test_discovery_metadata_cache_is_bounded(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.oidc.time.monotonic", lambda: clock[0])
    server = FakeOIDCServer()
    provider = make_provider(server)

    for offset in (0.0, 299.0, 301.0):
        clock[0] = 1000.0 + offset
        flow = generate_oidc_flow_secrets()
        await provider.authorization_url(
            state=flow.state,
            nonce=flow.nonce,
            code_challenge=flow.code_challenge,
            redirect_uri=REDIRECT_URI,
        )

    assert server.discovery_requests == 2


@pytest.mark.asyncio
async def test_valid_exchange_returns_only_safe_claims_and_forgets_provider_tokens(
    caplog,
):
    caplog.set_level(logging.DEBUG)
    server = FakeOIDCServer()
    provider = make_provider(server)

    claims = await provider.exchange_code(
        code=CODE,
        code_verifier=server.expected_verifier,
        redirect_uri=REDIRECT_URI,
        nonce=server.expected_nonce,
    )

    assert claims == OIDCClaims(
        issuer=ISSUER,
        subject="subject-123",
        audience=(CLIENT_ID,),
        expires_at=claims.expires_at,
        nonce=server.expected_nonce,
        display_name="Ada Operator",
        email="ada@example.test",
    )
    assert claims.expires_at > time.time()
    assert server.expected_nonce not in repr(claims)
    assert server.last_token_form == {
        "grant_type": ["authorization_code"],
        "redirect_uri": [REDIRECT_URI],
        "code": [CODE],
        "code_verifier": [server.expected_verifier],
    }
    expected_basic = base64.b64encode(
        f"{CLIENT_ID}:{CLIENT_SECRET}".encode("ascii")
    ).decode("ascii")
    assert server.last_authorization_header == f"Basic {expected_basic}"
    assert server.discovery_requests == 1
    assert server.jwks_requests == 1
    assert server.token_requests == 1

    # The fake server observes protocol credentials by design; the production
    # adapter itself retains no provider token or authorization code.
    persisted = list(all_provider_values(provider))
    assert CODE not in persisted
    assert ACCESS_TOKEN not in persisted
    assert REFRESH_TOKEN not in persisted
    rendered_logs = "\n".join(record.getMessage() for record in caplog.records)
    for forbidden in (
        CODE,
        server.expected_verifier,
        CLIENT_SECRET,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert forbidden not in rendered_logs


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://attacker.example.test"},
        {"aud": "different-client"},
        {"exp": 1},
        {"exp": float("nan")},
        {"exp": float("inf")},
        {"nonce": "wrong-nonce"},
        {"sub": ""},
        {"aud": [CLIENT_ID, "second-audience"]},
        {
            "aud": [CLIENT_ID, "second-audience"],
            "azp": "different-client",
        },
    ],
    ids=[
        "issuer",
        "audience",
        "expiry",
        "expiry-nan",
        "expiry-infinity",
        "nonce",
        "subject",
        "multiple-audience-missing-azp",
        "multiple-audience-wrong-azp",
    ],
)
@pytest.mark.asyncio
async def test_exchange_rejects_invalid_standard_claims(claim_overrides):
    server = FakeOIDCServer(claim_overrides=claim_overrides)
    provider = make_provider(server)

    with pytest.raises(OIDCValidationError) as exc_info:
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )

    assert str(exc_info.value) == "OIDC identity validation failed"
    assert CODE not in repr(exc_info.value)
    assert server.expected_verifier not in repr(exc_info.value)
    assert ACCESS_TOKEN not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_exchange_rejects_bad_signature_against_discovered_jwks():
    attacker_key = RSAKey.generate_key(
        2048,
        parameters={"kid": "provider-key", "alg": "RS256", "use": "sig"},
        private=True,
    )
    server = FakeOIDCServer(signing_key=attacker_key)
    provider = make_provider(server)

    with pytest.raises(OIDCValidationError, match="identity validation failed"):
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )


@pytest.mark.asyncio
async def test_same_kid_key_rotation_forces_one_jwks_refresh():
    server = FakeOIDCServer()
    provider = make_provider(server)
    first = await provider.exchange_code(
        code=CODE,
        code_verifier=server.expected_verifier,
        redirect_uri=REDIRECT_URI,
        nonce=server.expected_nonce,
    )
    assert first.subject == "subject-123"
    assert server.jwks_requests == 1

    replacement = RSAKey.generate_key(
        2048,
        parameters={"kid": "provider-key", "alg": "RS256", "use": "sig"},
        private=True,
    )
    server.public_key = replacement
    server.signing_key = replacement

    second = await provider.exchange_code(
        code=CODE,
        code_verifier=server.expected_verifier,
        redirect_uri=REDIRECT_URI,
        nonce=server.expected_nonce,
    )

    assert second.subject == "subject-123"
    assert server.jwks_requests == 2


@pytest.mark.asyncio
async def test_jwks_cache_expiry_stops_accepting_removed_key(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.oidc.time.monotonic", lambda: clock[0])
    server = FakeOIDCServer()
    provider = make_provider(server)
    assert (
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )
    ).subject == "subject-123"

    removed_signing_key = server.signing_key
    replacement = RSAKey.generate_key(
        2048,
        parameters={"kid": "replacement-key", "alg": "RS256", "use": "sig"},
        private=True,
    )
    server.public_key = replacement
    server.signing_key = removed_signing_key
    clock[0] += 301.0

    with pytest.raises(OIDCValidationError, match="identity validation failed"):
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )
    assert server.jwks_requests >= 2


@pytest.mark.asyncio
async def test_exchange_rejects_missing_id_token():
    server = FakeOIDCServer(token_overrides={"id_token": None})
    provider = make_provider(server)

    with pytest.raises(OIDCValidationError, match="identity validation failed"):
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )


@pytest.mark.parametrize(
    "token_overrides",
    [
        {"access_token": None},
        {"access_token": ""},
        {"token_type": None},
        {"token_type": "MAC"},
    ],
    ids=["missing-access-token", "blank-access-token", "missing-type", "wrong-type"],
)
@pytest.mark.asyncio
async def test_exchange_rejects_invalid_oauth_token_response_shape(token_overrides):
    server = FakeOIDCServer(token_overrides=token_overrides)
    provider = make_provider(server)

    with pytest.raises(OIDCValidationError, match="identity validation failed"):
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )


@pytest.mark.asyncio
async def test_discovery_rejects_unsigned_only_provider_metadata():
    server = FakeOIDCServer(
        metadata_overrides={"id_token_signing_alg_values_supported": ["none"]}
    )
    provider = make_provider(server)
    flow = generate_oidc_flow_secrets()

    with pytest.raises(OIDCDiscoveryError, match="provider discovery failed"):
        await provider.authorization_url(
            state=flow.state,
            nonce=flow.nonce,
            code_challenge=flow.code_challenge,
            redirect_uri=REDIRECT_URI,
        )
    assert server.token_requests == 0


@pytest.mark.parametrize(
    "metadata_overrides",
    [
        {"response_types_supported": ["id_token"]},
        {"code_challenge_methods_supported": ["plain"]},
        {"code_challenge_methods_supported": "S256"},
    ],
    ids=["no-code-flow", "no-s256", "malformed-pkce-capability"],
)
@pytest.mark.asyncio
async def test_discovery_rejects_incompatible_code_or_pkce_capabilities(
    metadata_overrides,
):
    server = FakeOIDCServer(metadata_overrides=metadata_overrides)
    provider = make_provider(server)
    flow = generate_oidc_flow_secrets()

    with pytest.raises(OIDCDiscoveryError, match="provider discovery failed"):
        await provider.authorization_url(
            state=flow.state,
            nonce=flow.nonce,
            code_challenge=flow.code_challenge,
            redirect_uri=REDIRECT_URI,
        )
    assert server.token_requests == 0


@pytest.mark.asyncio
async def test_metadata_issuer_mismatch_fails_before_authorization_or_exchange():
    server = FakeOIDCServer(
        metadata_overrides={"issuer": "https://different-issuer.example.test"}
    )
    provider = make_provider(server)
    flow = generate_oidc_flow_secrets()

    with pytest.raises(OIDCDiscoveryError) as exc_info:
        await provider.authorization_url(
            state=flow.state,
            nonce=flow.nonce,
            code_challenge=flow.code_challenge,
            redirect_uri=REDIRECT_URI,
        )

    assert str(exc_info.value) == "OIDC provider discovery failed"
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert CLIENT_SECRET not in repr(exc_info.value)
    assert server.token_requests == 0


@pytest.mark.asyncio
async def test_token_error_is_generic_and_does_not_echo_provider_description():
    server = FakeOIDCServer()
    provider = make_provider(server)

    with pytest.raises(OIDCTokenExchangeError) as exc_info:
        await provider.exchange_code(
            code="different-code",
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )

    assert str(exc_info.value) == "OIDC authorization code exchange failed"
    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert CODE not in repr(exc_info.value)
    assert "different-code" not in repr(exc_info.value)


@pytest.mark.asyncio
async def test_non_success_token_response_cannot_smuggle_valid_token_body():
    server = FakeOIDCServer(token_status_code=400)
    provider = make_provider(server)

    with pytest.raises(OIDCTokenExchangeError) as exc_info:
        await provider.exchange_code(
            code=CODE,
            code_verifier=server.expected_verifier,
            redirect_uri=REDIRECT_URI,
            nonce=server.expected_nonce,
        )

    assert exc_info.value.__context__ is None
    assert exc_info.value.__cause__ is None
    assert server.token_requests == 1
    assert server.jwks_requests == 0


def test_factory_protocol_and_configuration_validation_are_safe():
    server = FakeOIDCServer()
    provider = build_oidc_provider(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        metadata_url=METADATA_URL,
        scopes=("openid",),
        leeway=30,
        transport=server.transport,
    )
    assert isinstance(provider, OIDCProvider)
    assert repr(provider) == "AuthlibOIDCProvider(<configured>)"
    assert CLIENT_SECRET not in repr(provider)

    with pytest.raises(ValueError, match="include openid"):
        build_oidc_provider(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            scopes=("email",),
        )
    with pytest.raises(ValueError, match="HTTPS"):
        build_oidc_provider(
            issuer="http://issuer.invalid",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )
    with pytest.raises(ValueError, match="between zero and 300"):
        build_oidc_provider(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            leeway=301,
        )
    for leeway in (0, 300):
        assert isinstance(
            build_oidc_provider(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                leeway=leeway,
            ),
            OIDCProvider,
        )
