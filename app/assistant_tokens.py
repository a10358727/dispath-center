"""Per-turn assistant token lifecycle (DG-ASSISTANT-TOOLS v1 T-2, packet P1a).

Server A issues one short-lived ``dat_`` token at the start of a runner-hosted
assistant chat turn, bound to the speaking human actor, and revokes it when
the turn ends.  Only the digest is stored; the raw bearer exists in memory for
the SFTP write to the runner and is never logged or audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.audit import AuditActor, append_audit
from app.identity import generate_assistant_turn_token, redact_token


@dataclass(frozen=True)
class IssuedTurnToken:
    token_id: str
    raw_token: str = field(repr=False)
    expires_at: str

    def __str__(self) -> str:
        return redact_token(self.raw_token)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _audit(
    audit_path: str,
    action: str,
    params: dict[str, Any],
    actor: Optional[AuditActor],
) -> None:
    try:
        append_audit(action, params, path=audit_path, actor=actor)
    except Exception:  # noqa: BLE001 - audit is best-effort (INV-AUDIT-2)
        pass


def issue_assistant_turn_token(
    db: Any,
    *,
    actor_id: str,
    turn_ref: str,
    ttl_sec: int,
    audit_path: str,
    project_id: Optional[str] = None,
    now: Optional[datetime] = None,
    audit_actor: Optional[AuditActor] = None,
) -> IssuedTurnToken:
    """Persist a new token digest and return the raw bearer for delivery."""

    current = now or datetime.now(timezone.utc)
    issued = generate_assistant_turn_token()
    expires_at = _iso(current + timedelta(seconds=int(ttl_sec)))
    db.insert_assistant_turn_token(
        token_id=issued.id,
        secret_hash=issued.secret_hash,
        actor_id=actor_id,
        project_id=project_id,
        turn_ref=turn_ref,
        expires_at=expires_at,
        now=_iso(current),
    )
    _audit(
        audit_path,
        "assistant_turn_token_issue",
        {
            "token_id": issued.id,
            "actor_id": actor_id,
            "turn_ref": turn_ref,
            "expires_at": expires_at,
        },
        audit_actor,
    )
    return IssuedTurnToken(token_id=issued.id, raw_token=issued.raw_token, expires_at=expires_at)


def revoke_assistant_turn_token(
    db: Any,
    *,
    token_id: str,
    audit_path: str,
    now: Optional[datetime] = None,
    audit_actor: Optional[AuditActor] = None,
) -> bool:
    """Revoke a token at turn end; returns True when a live row was revoked."""

    changed = bool(db.revoke_assistant_turn_token(token_id, now=_iso(now or datetime.now(timezone.utc))))
    if changed:
        _audit(audit_path, "assistant_turn_token_revoke", {"token_id": token_id}, audit_actor)
    return changed


def purge_stale_assistant_turn_tokens(db: Any, *, keep_hours: int = 24, now: Optional[datetime] = None) -> int:
    """Bookkeeping: delete rows expired/revoked more than ``keep_hours`` ago."""

    cutoff = _iso((now or datetime.now(timezone.utc)) - timedelta(hours=int(keep_hours)))
    return int(db.purge_expired_assistant_turn_tokens(cutoff_iso=cutoff))


__all__ = [
    "IssuedTurnToken",
    "issue_assistant_turn_token",
    "purge_stale_assistant_turn_tokens",
    "revoke_assistant_turn_token",
]
