"""Image attachments for Studio session messages (DG-STUDIO-UI v1, P2-3).

Closed vocabulary and hard caps, validated on Server A before anything is
relayed; the runner turns them into SDK image content blocks. The persisted
event log stores metadata only — never the bytes (INV-STATE-1 keeps SQLite
the truth for *what happened*, not a blob store).
"""

from __future__ import annotations

import base64
import binascii
from typing import Any, Optional

MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024
ALLOWED_MEDIA_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


class InvalidAttachmentError(ValueError):
    """An attachment is outside the closed vocabulary or over the caps."""


def validate_attachments(raw: Optional[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not raw:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_ATTACHMENTS:
        raise InvalidAttachmentError(f"最多 {MAX_ATTACHMENTS} 個附件")
    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") != "image":
            raise InvalidAttachmentError("附件只支援 type=image")
        media_type = item.get("media_type")
        if media_type not in ALLOWED_MEDIA_TYPES:
            raise InvalidAttachmentError(f"media_type 必須是 {', '.join(ALLOWED_MEDIA_TYPES)}")
        data = item.get("data_base64")
        if not isinstance(data, str) or not data:
            raise InvalidAttachmentError("附件缺少 data_base64")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise InvalidAttachmentError("data_base64 不是合法的 base64") from exc
        if len(decoded) > MAX_ATTACHMENT_BYTES:
            raise InvalidAttachmentError(f"單一附件上限 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB")
        cleaned.append({"type": "image", "media_type": media_type, "data_base64": data, "bytes": len(decoded)})
    return cleaned


__all__ = ["ALLOWED_MEDIA_TYPES", "InvalidAttachmentError", "MAX_ATTACHMENTS", "MAX_ATTACHMENT_BYTES", "validate_attachments"]
