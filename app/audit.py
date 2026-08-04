"""稽核紀錄：append-only JSONL。

每行一筆 JSON：{ts, action, params, result}，新紀錄可選加入頂層
actor={id, kind, authentication}；歷史紀錄不會被補寫或改寫。
鐵律第 3 條要求「每個動作寫稽核」：enqueue、dispatch、done、failed、
requeue、reject 等等都要呼叫 append_audit()。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.identity import RequestContext

_write_lock = threading.Lock()
_health_lock = threading.Lock()
_logger = logging.getLogger("dispatch.audit")
_audit_health: dict[str, Any] = {
    "write_attempts": 0,
    "write_failures": 0,
    "consecutive_failures": 0,
    "last_success_at": None,
    "last_failure_at": None,
    "last_failure_category": None,
}


@dataclass(frozen=True)
class AuditActor:
    """Log-safe identity attribution for one newly appended audit record.

    This deliberately is not an ``Actor`` serializer.  Its three explicit
    fields form the complete public audit envelope, so actor email/display
    metadata and request credential details cannot be included accidentally.
    """

    id: str
    kind: str
    authentication: str

    def __post_init__(self) -> None:
        for field_name in ("id", "kind", "authentication"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"audit actor {field_name} must be a non-empty string")

    def to_envelope(self) -> dict[str, str]:
        """Return a fresh mapping containing only the approved actor fields."""

        return {
            "id": self.id,
            "kind": self.kind,
            "authentication": self.authentication,
        }


SYSTEM_AUDIT_ACTOR: Final = AuditActor(
    id="system",
    kind="system",
    authentication="system",
)


def audit_actor_from_request_context(
    context: RequestContext | None,
) -> AuditActor | None:
    """Project a request context onto the deliberately narrow audit envelope.

    Anonymous/development-open requests have no durable actor and therefore
    return ``None``.  In particular, this helper never serializes email,
    display name, channel/source labels, memberships, scopes, session IDs, or
    service-token IDs.
    """

    if context is None or context.actor is None:
        return None
    return AuditActor(
        id=context.actor.id,
        kind=context.actor.actor_type.value,
        authentication=context.authentication_method,
    )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_audit(
    action: str,
    params: dict[str, Any] | None = None,
    result: str = "ok",
    path: str | Path = "audit.jsonl",
    *,
    actor: AuditActor | None = None,
) -> dict[str, Any]:
    """寫入一筆稽核紀錄，回傳寫入的 record（方便測試/呼叫端立即使用）。"""
    record = {
        "ts": now_iso(),
        "action": action,
        "params": params or {},
        "result": result,
    }
    if actor is not None:
        record["actor"] = actor.to_envelope()
    line = json.dumps(record, ensure_ascii=False)
    try:
        with _write_lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as exc:
        # INV-AUDIT-2: audit evidence is best-effort and cannot abort the
        # already-authorized domain action. Failure is still operator-visible.
        observed_at = now_iso()
        with _health_lock:
            _audit_health["write_attempts"] += 1
            _audit_health["write_failures"] += 1
            _audit_health["consecutive_failures"] += 1
            _audit_health["last_failure_at"] = observed_at
            _audit_health["last_failure_category"] = type(exc).__name__
        _logger.error("audit append failed: %s", type(exc).__name__)
    else:
        observed_at = now_iso()
        with _health_lock:
            _audit_health["write_attempts"] += 1
            _audit_health["consecutive_failures"] = 0
            _audit_health["last_success_at"] = observed_at
    return record


def audit_health_snapshot() -> dict[str, Any]:
    """Return non-secret, process-local audit writer telemetry."""
    with _health_lock:
        snapshot = dict(_audit_health)
    return {
        **snapshot,
        "healthy": snapshot["consecutive_failures"] == 0,
        "delivery": "best_effort",
        "domain_action_aborted_on_failure": False,
    }


def read_audit(path: str | Path = "audit.jsonl") -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    records = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
    return records


def tail_audit(path: str | Path = "audit.jsonl", n: int = 100) -> list[dict[str, Any]]:
    """讀 audit.jsonl 尾 n 行，回傳新到舊（GET /events 用）。

    從檔尾以 bounded chunks 讀取，避免 `/events` 因歷史檔案增長而把整個
    JSONL 載入記憶體。檔案仍然是相容 read sink；durable DB events 由 API
    端優先查詢。
    """
    if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
        return []
    p = Path(path)
    if not p.exists():
        return []
    chunk_size = 8192
    data = b""
    with p.open("rb") as stream:
        stream.seek(0, 2)
        position = stream.tell()
        while position > 0 and data.count(b"\n") <= n:
            size = min(chunk_size, position)
            position -= size
            stream.seek(position)
            data = stream.read(size) + data
    lines = [line for line in data.splitlines() if line.strip()]
    records: list[dict[str, Any]] = []
    # A process can die after writing only part of the final JSONL line.  The
    # legacy sink is best-effort, so skip malformed tail lines and let the
    # durable DB/outbox remain authoritative; a later export retry writes a
    # complete line.  Decode errors in older lines are treated the same way
    # rather than taking down the read-only audit endpoint.
    for line in reversed(lines):
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
        if len(records) >= n:
            break
    return records


def deduplicate_audit_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove at-least-once export duplicates using stable event identity.

    Legacy records without an event ID are intentionally retained.  They have
    no durable provenance and cannot safely be guessed to be duplicates.
    """

    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        event_id = record.get("event_id")
        event_hash = record.get("event_sha256")
        if isinstance(event_id, str) and isinstance(event_hash, str):
            identity = (event_id, event_hash)
            if identity in seen:
                continue
            seen.add(identity)
        result.append(record)
    return result


def export_durable_audit_events(
    database: Any,
    path: str | Path = "audit.jsonl",
    *,
    limit: int = 100,
    owner: str = "audit-exporter",
    lease_seconds: int = 60,
    retry_seconds: int = 30,
    max_attempts: int = 5,
) -> dict[str, int]:
    """Export claimed DB events to JSONL and retain retry/dead-letter state.

    The DB event is committed before this function runs, so a read-only or
    full JSONL filesystem cannot erase the durable audit record. A crash after
    the append and before ``complete_durable_audit_export`` can produce an
    exact duplicate line on retry; consumers use the stable event ID/hash to
    de-duplicate, and no event is silently dropped.
    """

    claimed = database.claim_durable_audit_exports(
        owner=owner,
        limit=limit,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
    )
    exported = 0
    failed = 0
    dead_letter = 0
    for item in claimed:
        event = item["event"]
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        try:
            with _write_lock:
                with open(path, "a", encoding="utf-8") as stream:
                    stream.write(line + "\n")
        except OSError as exc:
            failed += 1
            state = database.fail_durable_audit_export(
                item["operation_id"],
                owner=owner,
                error_category=type(exc).__name__,
                sanitized_error_detail="JSONL export failed",
                retry_seconds=retry_seconds,
                max_attempts=max_attempts,
            )
            if state == "dead_letter":
                dead_letter += 1
            continue
        if database.complete_durable_audit_export(
            item["operation_id"], owner=owner
        ):
            exported += 1
    return {
        "claimed": len(claimed),
        "exported": exported,
        "failed": failed,
        "dead_letter": dead_letter,
    }
