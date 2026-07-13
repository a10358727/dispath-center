"""OIDC provider boundary and Authlib production adapter.

The HTTP routes own durable state/replay bookkeeping and server-side session
issuance.  This module owns only protocol inputs and the external provider
exchange.  In particular, an :class:`OIDCProvider` returns validated identity
claims, never the provider's access token, refresh token, or raw ID token.

The production adapter intentionally uses Authlib's low-level OAuth client for
the authorization URL.  Authlib's framework helper can generate a verifier and
write it to a DEBUG log; Dispatch Center generates PKCE material itself and
passes only the public S256 challenge to that step.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import math
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.errors import BadSignatureError, InvalidKeyIdError
from joserfc.jwk import KeySet


_URLSAFE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_PKCE_VERIFIER_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
_PKCE_VERIFIER_MIN_LENGTH = 43
_PKCE_VERIFIER_MAX_LENGTH = 128
_DEFAULT_SCOPES = ("openid", "profile", "email")
_MAX_CLOCK_SKEW_LEEWAY_SECONDS = 300
_METADATA_CACHE_TTL_SECONDS = 300.0
_JWKS_CACHE_TTL_SECONDS = 300.0

# Only asymmetric signing algorithms are accepted.  In particular, ``none``
# and client-secret-backed HMAC algorithms can never satisfy signature
# validation for an organizational OIDC issuer.
_ALLOWED_ID_TOKEN_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


@dataclass(frozen=True)
class OIDCFlowSecrets:
    """One login flow's raw browser-bound values.

    Every field is excluded from ``repr``.  The code challenge is public on
    the wire, but hiding the whole bundle makes accidental logging of its
    neighboring state, nonce, or verifier structurally harder.

    ``state`` is a composite ``<random>.<nonce>`` value.  The caller persists
    only hashes of the complete state and extracted nonce and can recover the
    expected raw nonce from the returned callback state after a process
    restart.  A separate Secure, HttpOnly handshake cookie must bind that
    callback value to the initiating browser.
    """

    state: str = field(repr=False)
    nonce: str = field(repr=False)
    code_verifier: str = field(repr=False)
    code_challenge: str = field(repr=False)


@dataclass(frozen=True)
class OIDCClaims:
    """Validated identity claims safe to return across the provider boundary."""

    issuer: str
    subject: str
    audience: tuple[str, ...]
    expires_at: int | float
    nonce: str = field(repr=False)
    display_name: Optional[str] = None
    email: Optional[str] = None


class OIDCProviderError(RuntimeError):
    """Base class whose message and repr never include provider material."""

    _safe_message = "OIDC provider operation failed"

    def __init__(self) -> None:
        super().__init__(self._safe_message)


class OIDCDiscoveryError(OIDCProviderError):
    """Discovery metadata or public key loading failed safely."""

    _safe_message = "OIDC provider discovery failed"


class OIDCTokenExchangeError(OIDCProviderError):
    """The authorization code exchange failed safely."""

    _safe_message = "OIDC authorization code exchange failed"


class OIDCValidationError(OIDCProviderError):
    """The provider response did not produce a valid OIDC identity."""

    _safe_message = "OIDC identity validation failed"


@runtime_checkable
class OIDCProvider(Protocol):
    """Injected provider surface used by the login and callback routes."""

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str: ...

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        nonce: str,
    ) -> OIDCClaims: ...


@dataclass(frozen=True)
class _ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    signing_algorithms: tuple[str, ...]
    token_endpoint_auth_method: str


def generate_oidc_flow_secrets() -> OIDCFlowSecrets:
    """Generate independent state, nonce, and RFC 7636 S256 PKCE inputs."""

    nonce = secrets.token_urlsafe(32)
    state_random = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(48)
    state = f"{state_random}.{nonce}"
    return OIDCFlowSecrets(
        state=state,
        nonce=nonce,
        code_verifier=code_verifier,
        code_challenge=pkce_s256_challenge(code_verifier),
    )


def extract_oidc_nonce(state: str) -> str:
    """Extract and validate the nonce carried by a generated state value."""

    if not isinstance(state, str):
        raise ValueError("invalid OIDC state")
    state_random, separator, nonce = state.partition(".")
    if (
        separator != "."
        or "." in nonce
        or not _is_urlsafe_secret(state_random)
        or not _is_urlsafe_secret(nonce)
    ):
        raise ValueError("invalid OIDC state")
    return nonce


def pkce_s256_challenge(code_verifier: str) -> str:
    """Return ``BASE64URL(SHA256(ASCII(verifier)))`` without padding."""

    if not _valid_pkce_verifier(code_verifier):
        raise ValueError("invalid PKCE verifier")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class AuthlibOIDCProvider:
    """Authorization Code + PKCE provider backed by Authlib 1.7.x.

    Discovery metadata and JWKS are loaded lazily and only their public,
    validated subsets are cached.  Each code exchange uses a short-lived
    Authlib client; the token mapping is cleared before the method returns or
    raises, and no provider token is attached to this long-lived adapter.
    """

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        metadata_url: Optional[str] = None,
        scopes: tuple[str, ...] = _DEFAULT_SCOPES,
        leeway: int = 0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 10.0,
    ) -> None:
        _require_nonempty(issuer)
        _require_nonempty(client_id)
        _require_nonempty(client_secret)
        _validate_https_url(issuer)
        if (
            not isinstance(scopes, tuple)
            or "openid" not in scopes
            or any(not isinstance(scope, str) or not scope for scope in scopes)
        ):
            raise ValueError("OIDC scopes must include openid")
        if (
            isinstance(leeway, bool)
            or not isinstance(leeway, int)
            or not 0 <= leeway <= _MAX_CLOCK_SKEW_LEEWAY_SECONDS
        ):
            raise ValueError("OIDC clock skew leeway must be between zero and 300")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("OIDC timeout must be positive")

        resolved_metadata_url = metadata_url or (
            issuer.rstrip("/") + "/.well-known/openid-configuration"
        )
        _validate_https_url(resolved_metadata_url)

        self._issuer = issuer
        self._client_id = client_id
        self._client_secret = client_secret
        self._metadata_url = resolved_metadata_url
        self._scopes = scopes
        self._leeway = leeway
        self._transport = transport
        self._timeout = float(timeout)
        self._metadata: Optional[_ProviderMetadata] = None
        self._metadata_loaded_at: Optional[float] = None
        self._key_set: Optional[KeySet] = None
        self._key_set_loaded_at: Optional[float] = None
        self._key_set_uri: Optional[str] = None
        self._metadata_lock = asyncio.Lock()
        self._jwks_lock = asyncio.Lock()

    def __repr__(self) -> str:
        return "AuthlibOIDCProvider(<configured>)"

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
        redirect_uri: str,
    ) -> str:
        _validate_protocol_input(state)
        _validate_protocol_input(nonce)
        if not _is_urlsafe_secret(code_challenge) or len(code_challenge) != 43:
            raise OIDCValidationError()
        _validate_redirect_uri(redirect_uri)

        failed = False
        try:
            metadata = await self._load_metadata()
            async with self._oauth_client(
                redirect_uri=redirect_uri,
                include_secret=False,
                metadata=metadata,
            ) as client:
                # Pass the already-derived public challenge.  Deliberately do
                # not configure Authlib's code_challenge_method or pass a
                # code_verifier to this operation: its framework helper logs
                # generated verifiers at DEBUG.
                url, returned_state = client.create_authorization_url(
                    metadata.authorization_endpoint,
                    state=state,
                    nonce=nonce,
                    code_challenge=code_challenge,
                    code_challenge_method="S256",
                )
        except Exception:
            # Raise only after leaving the exception handler.  Provider/client
            # exceptions may retain state, nonce, or request details in
            # ``__context__`` even when ``raise ... from None`` hides their
            # display; never attach that chain to our public safe exception.
            failed = True
        if failed:
            raise OIDCDiscoveryError()

        if not hmac.compare_digest(returned_state, state):
            raise OIDCValidationError()
        return url

    async def exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
        redirect_uri: str,
        nonce: str,
    ) -> OIDCClaims:
        _validate_protocol_input(code)
        if not _valid_pkce_verifier(code_verifier):
            raise OIDCValidationError()
        _validate_redirect_uri(redirect_uri)
        _validate_protocol_input(nonce)

        token: Optional[Mapping[str, Any]] = None
        validation_failed = False
        exchange_failed = False
        try:
            metadata = await self._load_metadata()
            async with self._oauth_client(
                redirect_uri=redirect_uri,
                include_secret=True,
                metadata=metadata,
            ) as client:
                token = await client.fetch_token(
                    metadata.token_endpoint,
                    grant_type="authorization_code",
                    code=code,
                    code_verifier=code_verifier,
                    redirect_uri=redirect_uri,
                )
                _validate_token_response(token)
                claims = await self._validate_id_token(
                    token,
                    metadata=metadata,
                    expected_nonce=nonce,
                )
        except OIDCValidationError:
            validation_failed = True
        except Exception:
            exchange_failed = True
        finally:
            # Authlib stores the parsed provider token on its short-lived
            # client and returns the same mutable mapping.  Clear that mapping
            # before either success or failure leaves this scope.
            if isinstance(token, dict):
                token.clear()

        if validation_failed:
            raise OIDCValidationError()
        if exchange_failed:
            raise OIDCTokenExchangeError()
        return claims

    def _oauth_client(
        self,
        *,
        redirect_uri: str,
        include_secret: bool,
        metadata: _ProviderMetadata,
    ) -> AsyncOAuth2Client:
        kwargs: dict[str, Any] = {
            "client_id": self._client_id,
            "scope": " ".join(self._scopes),
            "redirect_uri": redirect_uri,
            "timeout": self._timeout,
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        if include_secret:
            kwargs["client_secret"] = self._client_secret
            kwargs["token_endpoint_auth_method"] = (
                metadata.token_endpoint_auth_method
            )
        client = AsyncOAuth2Client(**kwargs)
        client.register_compliance_hook(
            "access_token_response", _require_success_token_response
        )
        return client

    async def _load_metadata(self) -> _ProviderMetadata:
        now = time.monotonic()
        if self._metadata_is_fresh(now):
            return self._metadata
        async with self._metadata_lock:
            now = time.monotonic()
            if self._metadata_is_fresh(now):
                return self._metadata
            provider_error = False
            unexpected_error = False
            try:
                raw = await self._get_json(self._metadata_url)
                metadata = self._validate_metadata(raw)
            except OIDCProviderError:
                provider_error = True
            except Exception:
                unexpected_error = True
            if provider_error or unexpected_error:
                raise OIDCDiscoveryError()
            self._metadata = metadata
            self._metadata_loaded_at = now
            return metadata

    def _metadata_is_fresh(self, now: float) -> bool:
        return bool(
            self._metadata is not None
            and self._metadata_loaded_at is not None
            and 0 <= now - self._metadata_loaded_at < _METADATA_CACHE_TTL_SECONDS
        )

    def _validate_metadata(self, raw: object) -> _ProviderMetadata:
        if not isinstance(raw, dict) or raw.get("issuer") != self._issuer:
            raise OIDCDiscoveryError()

        authorization_endpoint = raw.get("authorization_endpoint")
        token_endpoint = raw.get("token_endpoint")
        jwks_uri = raw.get("jwks_uri")
        for endpoint in (authorization_endpoint, token_endpoint, jwks_uri):
            if not isinstance(endpoint, str):
                raise OIDCDiscoveryError()
            endpoint_invalid = False
            try:
                _validate_https_endpoint_url(endpoint)
            except ValueError:
                endpoint_invalid = True
            if endpoint_invalid:
                raise OIDCDiscoveryError()

        response_types = raw.get("response_types_supported")
        if not isinstance(response_types, list) or "code" not in response_types:
            raise OIDCDiscoveryError()

        pkce_methods = raw.get("code_challenge_methods_supported")
        if pkce_methods is not None and (
            not isinstance(pkce_methods, list) or "S256" not in pkce_methods
        ):
            raise OIDCDiscoveryError()

        advertised_algorithms = raw.get("id_token_signing_alg_values_supported")
        if advertised_algorithms is None:
            advertised_algorithms = ["RS256"]
        if not isinstance(advertised_algorithms, list):
            raise OIDCDiscoveryError()
        signing_algorithms = tuple(
            algorithm
            for algorithm in advertised_algorithms
            if isinstance(algorithm, str)
            and algorithm in _ALLOWED_ID_TOKEN_ALGORITHMS
        )
        if not signing_algorithms:
            raise OIDCDiscoveryError()

        auth_methods = raw.get("token_endpoint_auth_methods_supported")
        if auth_methods is None:
            auth_method = "client_secret_basic"
        elif isinstance(auth_methods, list) and "client_secret_basic" in auth_methods:
            auth_method = "client_secret_basic"
        elif isinstance(auth_methods, list) and "client_secret_post" in auth_methods:
            auth_method = "client_secret_post"
        else:
            raise OIDCDiscoveryError()

        return _ProviderMetadata(
            issuer=self._issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            jwks_uri=jwks_uri,
            signing_algorithms=signing_algorithms,
            token_endpoint_auth_method=auth_method,
        )

    async def _load_key_set(
        self, metadata: _ProviderMetadata, *, force: bool = False
    ) -> KeySet:
        now = time.monotonic()
        if self._key_set_is_fresh(now, metadata.jwks_uri) and not force:
            return self._key_set
        async with self._jwks_lock:
            now = time.monotonic()
            if self._key_set_is_fresh(now, metadata.jwks_uri) and not force:
                return self._key_set
            failed = False
            try:
                raw = await self._get_json(metadata.jwks_uri)
                if not isinstance(raw, dict):
                    raise ValueError("invalid key set")
                key_set = KeySet.import_key_set(raw)
            except Exception:
                failed = True
            if failed:
                raise OIDCDiscoveryError()
            self._key_set = key_set
            self._key_set_loaded_at = now
            self._key_set_uri = metadata.jwks_uri
            return key_set

    def _key_set_is_fresh(self, now: float, jwks_uri: str) -> bool:
        return bool(
            self._key_set is not None
            and self._key_set_loaded_at is not None
            and self._key_set_uri == jwks_uri
            and 0 <= now - self._key_set_loaded_at < _JWKS_CACHE_TTL_SECONDS
        )

    async def _validate_id_token(
        self,
        token: Mapping[str, Any],
        *,
        metadata: _ProviderMetadata,
        expected_nonce: str,
    ) -> OIDCClaims:
        id_token = token.get("id_token") if isinstance(token, Mapping) else None
        if not isinstance(id_token, str) or not id_token:
            raise OIDCValidationError()

        key_set = await self._load_key_set(metadata)
        refresh_key_set = False
        invalid_signature = False
        try:
            decoded = jwt.decode(
                id_token,
                key=key_set,
                algorithms=metadata.signing_algorithms,
            )
        except (InvalidKeyIdError, BadSignatureError):
            refresh_key_set = True
        except Exception:
            invalid_signature = True

        if refresh_key_set:
            try:
                key_set = await self._load_key_set(metadata, force=True)
                decoded = jwt.decode(
                    id_token,
                    key=key_set,
                    algorithms=metadata.signing_algorithms,
                )
            except Exception:
                invalid_signature = True
        if invalid_signature:
            raise OIDCValidationError()

        access_token = token.get("access_token")
        params: dict[str, Any] = {
            "client_id": self._client_id,
            "nonce": expected_nonce,
        }
        if isinstance(access_token, str) and access_token:
            params["access_token"] = access_token
        options = {
            "iss": {"essential": True, "value": self._issuer},
            "aud": {"essential": True, "value": self._client_id},
        }
        invalid_claims = False
        try:
            claims = CodeIDToken(
                decoded.claims,
                decoded.header,
                options=options,
                params=params,
            )
            now = int(time.time())
            claims.validate(now=now, leeway=self._leeway)
            issuer = claims.get("iss")
            subject = claims.get("sub")
            audience = _normalize_audience(claims.get("aud"))
            expires_at = claims.get("exp")
            returned_nonce = claims.get("nonce")
            if (
                not isinstance(issuer, str)
                or issuer != self._issuer
                or not isinstance(subject, str)
                or not subject
                or self._client_id not in audience
                or not isinstance(expires_at, (int, float))
                or isinstance(expires_at, bool)
                or not math.isfinite(float(expires_at))
                or expires_at <= time.time() - self._leeway
                or not isinstance(returned_nonce, str)
                or not hmac.compare_digest(returned_nonce, expected_nonce)
            ):
                raise ValueError("invalid validated claims")
        except Exception:
            invalid_claims = True
        if invalid_claims:
            raise OIDCValidationError()

        return OIDCClaims(
            issuer=issuer,
            subject=subject,
            audience=audience,
            expires_at=expires_at,
            nonce=returned_nonce,
            display_name=_optional_claim_string(
                claims.get("name") or claims.get("preferred_username")
            ),
            email=_optional_claim_string(claims.get("email")),
        )

    async def _get_json(self, url: str) -> object:
        kwargs: dict[str, Any] = {
            "timeout": self._timeout,
            "follow_redirects": False,
            "headers": {"Accept": "application/json"},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        failed = False
        result: object = None
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.get(url)
                response.raise_for_status()
                result = response.json()
        except Exception:
            failed = True
        if failed:
            raise OIDCDiscoveryError()
        return result


def build_oidc_provider(
    *,
    issuer: str,
    client_id: str,
    client_secret: str,
    metadata_url: Optional[str] = None,
    scopes: tuple[str, ...] = _DEFAULT_SCOPES,
    leeway: int = 0,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    timeout: float = 10.0,
) -> OIDCProvider:
    """Build the production provider behind the injectable protocol."""

    return AuthlibOIDCProvider(
        issuer=issuer,
        client_id=client_id,
        client_secret=client_secret,
        metadata_url=metadata_url,
        scopes=scopes,
        leeway=leeway,
        transport=transport,
        timeout=timeout,
    )


def _is_urlsafe_secret(value: object) -> bool:
    return isinstance(value, str) and bool(value) and _URLSAFE_RE.fullmatch(value) is not None


def _valid_pkce_verifier(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and _PKCE_VERIFIER_RE.fullmatch(value) is not None
        and _PKCE_VERIFIER_MIN_LENGTH <= len(value) <= _PKCE_VERIFIER_MAX_LENGTH
    )


def _require_nonempty(value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("OIDC configuration value must not be blank")


def _validate_protocol_input(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise OIDCValidationError()


def _validate_https_url(value: str) -> None:
    _validate_https_endpoint_url(value)
    parts = urlsplit(value)
    if parts.query:
        raise ValueError("OIDC URL must be an absolute HTTPS URL")


def _validate_https_endpoint_url(value: str) -> None:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("OIDC URL must be an absolute HTTPS URL")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except (TypeError, ValueError) as exc:
        raise ValueError("OIDC URL must be an absolute HTTPS URL") from exc
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.hostname is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ValueError("OIDC URL must be an absolute HTTPS URL")


def _validate_redirect_uri(value: object) -> None:
    if not isinstance(value, str):
        raise OIDCValidationError()
    invalid = False
    try:
        _validate_https_url(value)
    except ValueError:
        invalid = True
    if invalid:
        raise OIDCValidationError()


def _normalize_audience(value: object) -> tuple[str, ...]:
    if isinstance(value, str) and value:
        return (value,)
    if isinstance(value, list) and value and all(
        isinstance(item, str) and item for item in value
    ):
        return tuple(value)
    raise ValueError("invalid audience")


def _optional_claim_string(value: object) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _require_success_token_response(response: httpx.Response) -> httpx.Response:
    """Reject non-2xx token bodies before Authlib parses token-shaped JSON."""

    if not 200 <= response.status_code < 300:
        response.raise_for_status()
    return response


def _validate_token_response(token: object) -> None:
    """Require the OAuth token fields needed for a bound OIDC code response."""

    if not isinstance(token, Mapping):
        raise OIDCValidationError()
    access_token = token.get("access_token")
    token_type = token.get("token_type")
    id_token = token.get("id_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(token_type, str)
        or token_type.casefold() != "bearer"
        or not isinstance(id_token, str)
        or not id_token
    ):
        raise OIDCValidationError()
