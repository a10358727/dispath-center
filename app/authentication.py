"""Credential resolution for Goal 1's compatible authentication transports.

This module has no HTTP, audit, authorization-policy, or side-effect hooks.
Session and service-token resolution only reads durable identity rows.  The
single write-capable helper is the explicit, idempotent bootstrap for the
reserved legacy shared-token actor; request-time resolution never creates or
updates principals and never records token use.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Protocol

from app.identity import (
    Actor,
    ActorSession,
    ActorType,
    ProjectMembership,
    ProjectRoleBinding,
    RequestContext,
    ServiceAccount,
    ServiceAccountToken,
    parse_service_token,
    parse_session_token,
    verify_secret,
)


# A deliberately reserved, stable UUID makes shared-token activity attributable
# across restarts without backfilling or fabricating historical audit rows.
LEGACY_ADMIN_ACTOR_ID = "00000000-0000-0000-0000-000000000001"
LEGACY_ADMIN_DISPLAY_NAME = "legacy-admin"


class LegacyActorCollisionError(RuntimeError):
    """The reserved ID exists but is not the exact compatibility principal."""


class IdentityDatabase(Protocol):
    """Narrow database surface used by credential resolution."""

    def get_actor(self, actor_id: str) -> Optional[Actor]: ...

    def insert_actor(
        self,
        *,
        actor_type: ActorType | str,
        display_name: str,
        actor_id: Optional[str] = None,
        email: Optional[str] = None,
        platform_admin: bool = False,
    ) -> Actor: ...

    def get_actor_session(self, session_id: str) -> Optional[ActorSession]: ...

    def get_service_account_token(
        self, token_id: str
    ) -> Optional[ServiceAccountToken]: ...

    def get_service_account(self, actor_id: str) -> Optional[ServiceAccount]: ...

    def list_project_memberships(
        self, *, project_id: Optional[str] = None, actor_id: Optional[str] = None
    ) -> list[ProjectMembership]: ...

    def list_project_role_bindings(
        self,
        *,
        project_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        active_only: bool = False,
    ) -> list[ProjectRoleBinding]: ...


def ensure_legacy_admin_actor(db: IdentityDatabase) -> Actor:
    """Idempotently bootstrap the fixed shared-token compatibility actor.

    A row occupying the reserved UUID is accepted only when it has the exact
    principal type, label, and platform-admin capability.  Conflicting data is
    never overwritten.  The ``IntegrityError`` path handles a concurrent
    bootstrap by loading and validating the winner.
    """

    actor = db.get_actor(LEGACY_ADMIN_ACTOR_ID)
    if actor is None:
        try:
            actor = db.insert_actor(
                actor_id=LEGACY_ADMIN_ACTOR_ID,
                actor_type=ActorType.LEGACY,
                display_name=LEGACY_ADMIN_DISPLAY_NAME,
                platform_admin=True,
            )
        except sqlite3.IntegrityError:
            actor = db.get_actor(LEGACY_ADMIN_ACTOR_ID)

    if not _is_expected_legacy_actor(actor):
        raise LegacyActorCollisionError(
            "reserved legacy-admin actor identity is occupied by conflicting data"
        )
    return actor


def resolve_session_context(
    db: IdentityDatabase,
    raw_session: Optional[str],
    *,
    now: Optional[datetime] = None,
    project_roles_v2_enabled: bool = False,
    allow_high_risk_self_approval: bool = False,
) -> Optional[RequestContext]:
    """Resolve an active server-side session without modifying durable state."""

    if not raw_session:
        return None
    try:
        session_id, _ = parse_session_token(raw_session)
        session = db.get_actor_session(session_id)
    except (TypeError, ValueError):
        return None

    if session is None or session.revoked_at is not None:
        return None
    if not verify_secret(raw_session, session.secret_hash):
        return None
    if not _is_unexpired(session.expires_at, now):
        return None
    return _actor_context(
        db,
        actor_id=session.actor_id,
        authentication_method="session",
        expected_actor_type=ActorType.HUMAN,
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )


def resolve_service_token_context(
    db: IdentityDatabase,
    raw_token: Optional[str],
    *,
    enabled: bool,
    now: Optional[datetime] = None,
    project_roles_v2_enabled: bool = False,
    allow_high_risk_self_approval: bool = False,
) -> Optional[RequestContext]:
    """Resolve an enabled, active service bearer token without touching it."""

    if not enabled or not raw_token:
        return None
    try:
        token_id, _ = parse_service_token(raw_token)
        token = db.get_service_account_token(token_id)
    except (TypeError, ValueError):
        return None

    if token is None or token.revoked_at is not None:
        return None
    if not verify_secret(raw_token, token.secret_hash):
        return None
    if not _is_unexpired(token.expires_at, now):
        return None
    if not _valid_scopes(token.scopes):
        return None

    try:
        actor = db.get_actor(token.service_account_actor_id)
        account = db.get_service_account(token.service_account_actor_id)
        memberships = db.list_project_memberships(actor_id=token.service_account_actor_id)
        role_bindings = _load_role_bindings(
            db, actor_id=token.service_account_actor_id
        )
    except (TypeError, ValueError):
        return None
    if (
        actor is None
        or actor.actor_type is not ActorType.SERVICE
        or actor.disabled_at is not None
        or account is None
    ):
        return None
    return RequestContext(
        actor=actor,
        authentication_method="service_token",
        service_token_id=token.id,
        service_scopes=frozenset(token.scopes),
        project_memberships=tuple(memberships),
        project_role_bindings=tuple(role_bindings),
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )


def resolve_legacy_token_context(
    db: IdentityDatabase,
    presented_token: Optional[str],
    *,
    configured_token: Optional[str],
    enabled: bool,
    project_roles_v2_enabled: bool = False,
    allow_high_risk_self_approval: bool = False,
) -> Optional[RequestContext]:
    """Resolve the shared token to its pre-bootstrapped durable actor."""

    if not enabled or not _shared_tokens_equal(presented_token, configured_token):
        return None
    try:
        actor = db.get_actor(LEGACY_ADMIN_ACTOR_ID)
    except (TypeError, ValueError):
        return None
    if not _is_expected_legacy_actor(actor):
        return None
    if actor.disabled_at is not None:
        return None
    try:
        memberships = db.list_project_memberships(actor_id=actor.id)
        role_bindings = _load_role_bindings(db, actor_id=actor.id)
    except (TypeError, ValueError):
        return None
    return RequestContext(
        actor=actor,
        authentication_method="legacy_shared_token",
        project_memberships=tuple(memberships),
        project_role_bindings=tuple(role_bindings),
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )


def resolve_request_context(
    db: IdentityDatabase,
    *,
    session_token: Optional[str] = None,
    authorization: Optional[str] = None,
    legacy_token: Optional[str] = None,
    configured_legacy_token: Optional[str] = None,
    service_token_auth_enabled: bool = False,
    legacy_shared_token_enabled: bool = True,
    project_roles_v2_enabled: bool = False,
    allow_high_risk_self_approval: bool = False,
    now: Optional[datetime] = None,
) -> Optional[RequestContext]:
    """Resolve credentials in fixed session, service, then legacy precedence.

    ``now`` must be a timezone-aware :class:`datetime`.  It exists to make
    expiry tests deterministic; when omitted, current UTC time is used.
    Invalid credentials at one transport do not prevent a valid lower-priority
    compatibility transport from being considered.
    """

    context = resolve_session_context(
        db,
        session_token,
        now=now,
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )
    if context is not None:
        return context

    bearer_token = extract_bearer_token(authorization)
    context = resolve_service_token_context(
        db,
        bearer_token,
        enabled=service_token_auth_enabled,
        now=now,
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )
    if context is not None:
        return context

    return resolve_legacy_token_context(
        db,
        legacy_token,
        configured_token=configured_legacy_token,
        enabled=legacy_shared_token_enabled,
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )


def extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract an exact single Bearer credential without echoing bad input."""

    if not isinstance(authorization, str):
        return None
    parts = authorization.strip().split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def _actor_context(
    db: IdentityDatabase,
    *,
    actor_id: str,
    authentication_method: str,
    expected_actor_type: Optional[ActorType] = None,
    project_roles_v2_enabled: bool = False,
    allow_high_risk_self_approval: bool = False,
) -> Optional[RequestContext]:
    try:
        actor = db.get_actor(actor_id)
        memberships = db.list_project_memberships(actor_id=actor_id)
        role_bindings = _load_role_bindings(db, actor_id=actor_id)
    except (TypeError, ValueError):
        return None
    if (
        actor is None
        or actor.disabled_at is not None
        or (
            expected_actor_type is not None
            and actor.actor_type is not expected_actor_type
        )
    ):
        return None
    return RequestContext(
        actor=actor,
        authentication_method=authentication_method,
        project_memberships=tuple(memberships),
        project_role_bindings=tuple(role_bindings),
        project_roles_v2_enabled=project_roles_v2_enabled,
        allow_high_risk_self_approval=allow_high_risk_self_approval,
    )


def _is_unexpired(expires_at: str, now: Optional[datetime]) -> bool:
    current = _current_time(now)
    if not isinstance(expires_at, str) or not expires_at:
        return False
    normalized = expires_at[:-1] + "+00:00" if expires_at.endswith("Z") else expires_at
    try:
        expiry = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        return False
    return expiry.astimezone(timezone.utc) > current


def _load_role_bindings(
    db: IdentityDatabase,
    *,
    actor_id: str,
) -> list[ProjectRoleBinding]:
    """Keep older protocol fakes compatible while the v2 field is additive."""

    loader = getattr(db, "list_project_role_bindings", None)
    if not callable(loader):
        return []
    return loader(actor_id=actor_id, active_only=True)


def _current_time(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a timezone-aware datetime")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(timezone.utc)


def _shared_tokens_equal(
    presented_token: Optional[str], configured_token: Optional[str]
) -> bool:
    if (
        not isinstance(presented_token, str)
        or not presented_token
        or not isinstance(configured_token, str)
        or not configured_token
    ):
        return False
    # Compare fixed-length digests so Unicode values are supported without the
    # type/encoding restrictions of ``compare_digest(str, str)``.
    presented_digest = hashlib.sha256(presented_token.encode("utf-8")).digest()
    configured_digest = hashlib.sha256(configured_token.encode("utf-8")).digest()
    return hmac.compare_digest(presented_digest, configured_digest)


def _valid_scopes(scopes: object) -> bool:
    return isinstance(scopes, list) and all(
        isinstance(scope, str) and bool(scope) for scope in scopes
    )


def _is_expected_legacy_actor(actor: Optional[Actor]) -> bool:
    return bool(
        actor is not None
        and actor.id == LEGACY_ADMIN_ACTOR_ID
        and actor.actor_type is ActorType.LEGACY
        and actor.display_name == LEGACY_ADMIN_DISPLAY_NAME
        and actor.platform_admin is True
    )
