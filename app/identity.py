"""Durable actor identity models and secret-handling primitives.

This module deliberately contains no HTTP, database, OIDC-network, or
authorization behavior.  It defines the values persisted by Goal 1's additive
schema and the small, deterministic credential primitives shared by later
slices.

Raw session and service-token secrets are high-entropy bearer credentials.
Callers may return them once at issuance, but must persist only
``hash_secret(...)``.  Credential hashes and PKCE material are excluded from
the representations of the dataclasses below so routine logging cannot expose
them accidentally.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


DEFAULT_SECRET_BYTES = 32
SERVICE_TOKEN_PREFIX = "dcs_"
SESSION_TOKEN_PREFIX = "dcsess_"
#: Goal 3 C2（INV-NODE-1）：Node Agent 憑證前綴。刻意與人類 session／
#: service token 分開——node 身分不是人也不是一般自動化帳號，洩漏的處置是
#: 撤銷該 node，不影響其他 node，也不會讓持有者取得任何人類/服務權限。
NODE_TOKEN_PREFIX = "dcn_"
REDACTED = "<redacted>"
_URLSAFE_TOKEN_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


class ActorType(str, Enum):
    """Durable principal kinds supported by Goal 1."""

    HUMAN = "human"
    SERVICE = "service"
    LEGACY = "legacy"

    def __str__(self) -> str:
        return self.value


class ProjectRole(str, Enum):
    """Project membership roles used by the later policy evaluator."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    def __str__(self) -> str:
        return self.value


class ProjectRoleV2(str, Enum):
    """Independent Product v2 roles; legacy ``admin`` is not a v2 role."""

    OWNER = "owner"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    DATASET_MANAGER = "dataset_manager"
    VIEWER = "viewer"

    def __str__(self) -> str:
        return self.value


class ProjectRoleGrantProvenance(str, Enum):
    """Closed provenance catalog for immutable role grants."""

    LEGACY_MEMBERSHIP = "legacy_membership"
    APPROVED_ROLE_CHANGE = "approved_role_change"
    PROJECT_BOOTSTRAP = "project_bootstrap"

    def __str__(self) -> str:
        return self.value


@dataclass
class Actor:
    id: str
    actor_type: ActorType
    display_name: str
    email: Optional[str] = None
    platform_admin: bool = False
    disabled_at: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        # SQLite rows contain strings.  Normalize them at the domain boundary
        # so invalid stored or caller-provided values fail explicitly.
        self.actor_type = ActorType(self.actor_type)


@dataclass
class OIDCIdentity:
    id: str
    actor_id: str
    issuer: str
    subject: str
    email: Optional[str] = None
    created_at: str = ""
    last_authenticated_at: Optional[str] = None


@dataclass
class OIDCLoginFlow:
    state_hash: str = field(repr=False)
    nonce_hash: str = field(repr=False)
    created_at: str = ""
    expires_at: str = ""
    pkce_verifier: Optional[str] = field(default=None, repr=False)
    return_to: Optional[str] = None
    consumed_at: Optional[str] = None


@dataclass
class ActorSession:
    id: str
    actor_id: str
    secret_hash: str = field(repr=False)
    created_at: str = ""
    expires_at: str = ""
    oidc_identity_id: Optional[str] = None
    revoked_at: Optional[str] = None


@dataclass
class ServiceAccount:
    actor_id: str
    name: str
    created_at: str = ""
    description: Optional[str] = None
    created_by_actor_id: Optional[str] = None


@dataclass
class ServiceAccountToken:
    id: str
    service_account_actor_id: str
    secret_hash: str = field(repr=False)
    created_at: str = ""
    expires_at: str = ""
    scopes: list[str] = field(default_factory=list)
    label: Optional[str] = None
    created_by_actor_id: Optional[str] = None
    last_used_at: Optional[str] = None
    revoked_at: Optional[str] = None


@dataclass
class ProjectMembership:
    project_id: str
    actor_id: str
    role: ProjectRole
    created_at: str = ""
    updated_at: str = ""
    created_by_actor_id: Optional[str] = None

    def __post_init__(self) -> None:
        self.role = ProjectRole(self.role)


@dataclass
class ProjectRoleBinding:
    """One immutable Product v2 role grant and optional revocation evidence."""

    id: str
    project_id: str
    actor_id: str
    role: ProjectRoleV2
    grant_provenance: ProjectRoleGrantProvenance
    granted_at: str
    grant_approval_id: Optional[int] = None
    revocation_approval_id: Optional[int] = None
    revoked_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.role = ProjectRoleV2(self.role)
        self.grant_provenance = ProjectRoleGrantProvenance(self.grant_provenance)
        if (self.revocation_approval_id is None) != (self.revoked_at is None):
            raise ValueError(
                "revocation_approval_id and revoked_at must be present together"
            )

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True)
class RequestContext:
    """Authenticated principal data propagated through one request.

    ``source`` values such as ``web`` or ``chatgpt`` intentionally do not
    appear here: they describe a channel, not identity.  Collections are
    normalized to immutable containers so the frozen context remains stable
    while it is propagated through async request and tool paths.
    """

    actor: Optional[Actor] = None
    authentication_method: str = "anonymous"
    service_token_id: Optional[str] = None
    service_scopes: frozenset[str] = field(default_factory=frozenset)
    project_memberships: tuple[ProjectMembership, ...] = field(default_factory=tuple)
    project_role_bindings: tuple[ProjectRoleBinding, ...] = field(default_factory=tuple)
    project_roles_v2_enabled: bool = False
    allow_high_risk_self_approval: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.authentication_method, str) or not self.authentication_method:
            raise ValueError("authentication_method must be a non-empty string")
        object.__setattr__(self, "service_scopes", frozenset(self.service_scopes))
        object.__setattr__(self, "project_memberships", tuple(self.project_memberships))
        object.__setattr__(
            self, "project_role_bindings", tuple(self.project_role_bindings)
        )
        if not isinstance(self.project_roles_v2_enabled, bool):
            raise ValueError("project_roles_v2_enabled must be a boolean")
        if not isinstance(self.allow_high_risk_self_approval, bool):
            raise ValueError("allow_high_risk_self_approval must be a boolean")

    @property
    def actor_id(self) -> Optional[str]:
        return self.actor.id if self.actor is not None else None

    @property
    def actor_type(self) -> Optional[ActorType]:
        return self.actor.actor_type if self.actor is not None else None

    @property
    def platform_admin(self) -> bool:
        return bool(self.actor is not None and self.actor.platform_admin)


@dataclass(frozen=True)
class IssuedServiceToken:
    """One-time service-token issuance result.

    ``raw_token`` is intentionally accessible to the issuance response, but
    both it and its hash are omitted from ``repr``.  ``str(result)`` returns a
    safe identifier-bearing redaction rather than the bearer credential.
    """

    id: str
    raw_token: str = field(repr=False)
    secret_hash: str = field(repr=False)

    def __str__(self) -> str:
        return redact_token(self.raw_token)


@dataclass(frozen=True)
class IssuedSessionToken:
    """One-time server-side-session credential.

    The UUID is only a lookup identifier.  Possession is proved by hashing and
    comparing the complete ``raw_token``; neither the raw value nor its digest
    is included in routine representations.
    """

    id: str
    raw_token: str = field(repr=False)
    secret_hash: str = field(repr=False)

    def __str__(self) -> str:
        return redact_session_token(self.raw_token)


def generate_secret(num_bytes: int = DEFAULT_SECRET_BYTES) -> str:
    """Return a URL-safe bearer secret backed by ``num_bytes`` of entropy."""

    if isinstance(num_bytes, bool) or not isinstance(num_bytes, int):
        raise TypeError("num_bytes must be an integer")
    if num_bytes <= 0:
        raise ValueError("num_bytes must be greater than zero")
    return secrets.token_urlsafe(num_bytes)


def hash_secret(secret: str) -> str:
    """Return the lowercase hexadecimal SHA-256 digest for a bearer secret."""

    if not isinstance(secret, str):
        raise TypeError("secret must be a string")
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str, expected_hash: str) -> bool:
    """Compare a secret with a stored SHA-256 digest in constant time."""

    if not isinstance(secret, str) or not isinstance(expected_hash, str):
        return False
    return hmac.compare_digest(hash_secret(secret), expected_hash)


def generate_service_token(token_id: Optional[str] = None) -> IssuedServiceToken:
    """Create a one-time service token in ``dcs_<uuid>.<secret>`` form.

    The digest covers the complete bearer token.  Authentication can parse the
    public token ID for lookup, then verify the supplied raw token against the
    stored digest without persisting either the secret component or raw token.
    """

    if token_id is None:
        normalized_id = str(uuid.uuid4())
    else:
        normalized_id = _normalize_uuid(token_id)

    raw_token = f"{SERVICE_TOKEN_PREFIX}{normalized_id}.{generate_secret()}"
    return IssuedServiceToken(
        id=normalized_id,
        raw_token=raw_token,
        secret_hash=hash_secret(raw_token),
    )


def generate_session_token(session_id: Optional[str] = None) -> IssuedSessionToken:
    """Create an opaque session credential in ``dcsess_<uuid>.<secret>`` form.

    The returned raw value is suitable for a one-time ``Set-Cookie`` response.
    Callers must persist only ``secret_hash`` alongside ``id``.
    """

    if session_id is None:
        normalized_id = str(uuid.uuid4())
    else:
        normalized_id = _normalize_session_uuid(session_id)

    raw_token = f"{SESSION_TOKEN_PREFIX}{normalized_id}.{generate_secret()}"
    return IssuedSessionToken(
        id=normalized_id,
        raw_token=raw_token,
        secret_hash=hash_secret(raw_token),
    )


@dataclass
class IssuedNodeToken:
    """Goal 3 C2（INV-NODE-1）：一次性的 node 憑證核發結果。

    與 :class:`IssuedServiceToken` 同構——`raw_token` 只在核發當下回傳一次，
    永遠不落庫、不進稽核、不進日誌；持久化的只有 `secret_hash`。
    """

    id: str
    raw_token: str = field(repr=False)
    secret_hash: str = field(repr=False)

    def __str__(self) -> str:
        return redact_node_token(self.raw_token)


def generate_node_token(node_id: Optional[str] = None) -> IssuedNodeToken:
    """Create a one-time node credential in ``dcn_<uuid>.<secret>`` form."""

    if node_id is None:
        normalized_id = str(uuid.uuid4())
    else:
        normalized_id = _normalize_node_uuid(node_id)

    raw_token = f"{NODE_TOKEN_PREFIX}{normalized_id}.{generate_secret()}"
    return IssuedNodeToken(
        id=normalized_id,
        raw_token=raw_token,
        secret_hash=hash_secret(raw_token),
    )


def parse_node_token(raw_token: str) -> tuple[str, str]:
    """Return ``(node_id, secret)`` for a syntactically valid node credential.

    Format validation only; callers must still load the node row and use
    :func:`verify_secret`, plus check the node is not revoked.
    """

    if not isinstance(raw_token, str):
        raise ValueError("invalid node token")

    identifier, separator, secret = raw_token.partition(".")
    if (
        not separator
        or not identifier.startswith(NODE_TOKEN_PREFIX)
        or not secret
        or "." in secret
        or any(character not in _URLSAFE_TOKEN_CHARACTERS for character in secret)
    ):
        raise ValueError("invalid node token")

    node_id = _normalize_node_uuid(identifier[len(NODE_TOKEN_PREFIX) :])
    return node_id, secret


def redact_node_token(token: Optional[str]) -> str:
    """Return a log-safe node-token representation that never includes a secret."""

    if token:
        try:
            node_id, _ = parse_node_token(token)
        except ValueError:
            return REDACTED
        return f"{NODE_TOKEN_PREFIX}{node_id}.{REDACTED}"
    return REDACTED


def parse_service_token(raw_token: str) -> tuple[str, str]:
    """Return ``(token_id, secret)`` for a syntactically valid service token.

    Parsing performs format validation only; callers must still load the token
    row and use :func:`verify_secret` for authentication.
    """

    if not isinstance(raw_token, str):
        raise ValueError("invalid service token")

    identifier, separator, secret = raw_token.partition(".")
    if (
        not separator
        or not identifier.startswith(SERVICE_TOKEN_PREFIX)
        or not secret
        or "." in secret
        or any(character not in _URLSAFE_TOKEN_CHARACTERS for character in secret)
    ):
        raise ValueError("invalid service token")

    token_id = _normalize_uuid(identifier[len(SERVICE_TOKEN_PREFIX) :])
    return token_id, secret


def parse_session_token(raw_token: str) -> tuple[str, str]:
    """Return ``(session_id, secret)`` for a valid session credential."""

    if not isinstance(raw_token, str):
        raise ValueError("invalid session token")

    identifier, separator, secret = raw_token.partition(".")
    if (
        not separator
        or not identifier.startswith(SESSION_TOKEN_PREFIX)
        or not secret
        or "." in secret
        or any(character not in _URLSAFE_TOKEN_CHARACTERS for character in secret)
    ):
        raise ValueError("invalid session token")

    session_id = _normalize_session_uuid(identifier[len(SESSION_TOKEN_PREFIX) :])
    return session_id, secret


def redact_token(token: Optional[str]) -> str:
    """Return a log-safe token representation that never includes a secret."""

    if token:
        try:
            token_id, _ = parse_service_token(token)
        except ValueError:
            pass
        else:
            return f"{SERVICE_TOKEN_PREFIX}{token_id}.{REDACTED}"
    return REDACTED


def redact_session_token(token: Optional[str]) -> str:
    """Return a log-safe session representation without cookie secret bytes."""

    if token:
        try:
            session_id, _ = parse_session_token(token)
        except ValueError:
            pass
        else:
            return f"{SESSION_TOKEN_PREFIX}{session_id}.{REDACTED}"
    return REDACTED


def _normalize_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid service token id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid service token id") from None
    return str(parsed)


def _normalize_node_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid node id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid node id") from None
    return str(parsed)


def _normalize_session_uuid(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid session id")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid session id") from None
    return str(parsed)
