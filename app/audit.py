"""稽核紀錄：append-only JSONL。

每行一筆 JSON：{ts, action, params, result}，新紀錄可選加入頂層
actor={id, kind, authentication}；歷史紀錄不會被補寫或改寫。
鐵律第 3 條要求「每個動作寫稽核」：enqueue、dispatch、done、failed、
requeue、reject 等等都要呼叫 append_audit()。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.identity import RequestContext

_write_lock = threading.Lock()


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
    except OSError:
        # INV-AUDIT-2: audit evidence is best-effort and cannot abort the
        # already-authorized domain action.
        pass
    return record


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

    稽核檔案量本階段不大，直接讀全部再切尾巴＋反轉即可，不做真正的
    「從檔尾往回讀」最佳化。
    """
    records = read_audit(path)
    return list(reversed(records[-n:]))
